"""Live-ClickHouse proof that a reconcile alone isolates an org's identity.

`test_ch_rbac_isolation.py` builds its DDL by hand, so it would pass even if the
engine rendered nothing. This drives the real path instead - org registry + group
store -> `derive_group_bindings` -> `reconcile` - and connects as the users the
reconciler itself minted.

A reconcile is cluster-global: it sweeps stale `dfe_org_*` users and
`dfe_rowpol_*` policies. On the harness's own per-test ClickHouse nothing else is
there to sweep; on a shared cluster, run this serially (`-n 0`), and the fixture
refuses to run where it would destroy objects it did not create.

No mocks (project policy): a real CH, a real file-backed secrets store.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from dfe_engine.governance.ch.models import TENANT_ROLE, org_user_name
from dfe_engine.schema.plan import render_one

from .conftest import count_as, drop_safely

pytestmark = pytest.mark.integration

# The manifest objects the reconcile's dfe_meta projection writes into.
_META_IDS = ("db.meta", "meta.orgs", "meta.ch_tiers")


@pytest.fixture
def reconciled_world(ch_client, ch_params, tmp_path_factory):
    """Reconcile a uid-scoped world through the real entry point.

    Every object is uid-suffixed and dropped in teardown. The reconcile also
    rebuilds the ``dfe_meta`` projection, which is ordinary reconcile behaviour
    (gitops stays the source of truth), so it is left in place.
    """
    from dfe_engine.governance.ch.bindings import derive_group_bindings
    from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES, ChTier
    from dfe_engine.governance.ch.reconciler import ChRbacReconciler
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import SecretsSettings

    try:
        ch_client.command("SET SQL_current_tenant_id = 'probe'")
    except Exception:
        pytest.skip("server does not allow the SQL_ custom-settings prefix")

    uid = uuid.uuid4().hex[:10]
    db = f"dfe_test_rec_{uid}"
    table = "events"
    table_fqn = f"{db}.{table}"
    org_a = f"orga{uid}"
    org_b = f"orgb{uid}"

    foreign = [
        r[0]
        for r in ch_client.query("SELECT name FROM system.users").result_rows
        if r[0].startswith(("dfe_org_", "dfe_grp_")) and uid not in r[0]
    ]
    if foreign:
        pytest.skip(
            f"refusing to reconcile: cluster carries tenant users we did not create: {foreign}"
        )

    g_scoped = f"grp-scoped-{uid}"  # customer's-customer group -> pinned
    g_open = f"grp-open-{uid}"  # no org -> unrestricted
    g_analyst = f"grp-analyst-{uid}"  # platform role + org markers -> unrestricted
    g_admin = f"grp-admin-{uid}"  # admin -> unrestricted + otel_reader composed

    orgs = [
        SimpleNamespace(name=org_a, org_ids=[org_a]),
        SimpleNamespace(name=org_b, org_ids=[org_b]),
    ]
    groups = [
        SimpleNamespace(name=g_scoped, scope_org=org_a, org_ids=[], roles=["org_viewer"]),
        SimpleNamespace(name=g_open, scope_org="", org_ids=[], roles=[]),
        SimpleNamespace(name=g_analyst, scope_org="", org_ids=[org_a], roles=["data_analyst"]),
        SimpleNamespace(name=g_admin, scope_org="", org_ids=[], roles=["admin"]),
    ]
    # A uid-scoped tier so the users can read the test table; the seeded tiers'
    # names are not uid-scoped. Mirrors the seeded analyst tiers (D9): a BROAD grant
    # on the whole data db `dfe` (so a platform user reads every source table incl
    # dfe.otel_*) PLUS the uid test db that holds this fixture's own events table.
    # Isolation is by row policy, not grant scope.
    tier = ChTier(
        name=f"test{uid}",
        kind="analyst",
        default=True,
        grants=[f"SELECT ON {db}.*", "SELECT ON dfe.*"],
        # Mirrors the seeded analyst tiers: readonly=2 keeps queries read-only
        # while BI clients can still set per-query output settings.
        settings={"readonly": 2},
        quota={"interval": "1 hour", "queries": 1000},
    )
    users = [org_user_name(org_a), org_user_name(org_b)] + [
        f"dfe_grp_{g}" for g in (g_scoped, g_open, g_analyst, g_admin)
    ]
    # The otel telemetry tables live in the `dfe` data db (dfe.otel_*), not a
    # standalone `otel` db. A platform user reads them via the broad dfe.* grant; a
    # fenced tenant user reaches dfe.* too, but the `USING 0` deny policy on the
    # non-_org_id otel table returns zero rows.
    otel_table = f"dfe.otel_probe_{uid}"

    def _drop_all() -> None:
        for u in users:
            drop_safely(ch_client, f"DROP USER IF EXISTS `{u}`")
        # The reconcile policies every _org_id table it discovers, cluster-wide,
        # not just this test's own db - sweep them ALL. Safe because the fixture
        # refuses to run at all where foreign tenant users exist.
        try:
            existing = ch_client.query(
                "SELECT short_name, database, table FROM system.row_policies "
                "WHERE short_name LIKE 'dfe_rowpol_tenant_%'"
            ).result_rows
        except Exception:
            existing = []
        for short_name, pdb, ptable in existing:
            drop_safely(
                ch_client, f"DROP ROW POLICY IF EXISTS `{short_name}` ON `{pdb}`.`{ptable}`"
            )
        drop_safely(ch_client, f"DROP ROLE IF EXISTS {tier.role()}")
        drop_safely(ch_client, f"DROP SETTINGS PROFILE IF EXISTS {tier.profile()}")
        drop_safely(ch_client, f"DROP QUOTA IF EXISTS {tier.quota_name()}")
        drop_safely(ch_client, f"DROP DATABASE IF EXISTS {db}")
        # Drop only the uid-scoped otel probe table, never the shared `dfe` db.
        drop_safely(ch_client, f"DROP TABLE IF EXISTS {otel_table}")
        drop_safely(ch_client, "DROP ROLE IF EXISTS dfe_otel_reader_role")

    try:
        # The reconcile writes the dfe_meta projection, which the schema phase creates.
        for object_id in _META_IDS:
            for statement in render_one(object_id).statements:
                ch_client.command(statement)
        ch_client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
        ch_client.command(
            f"CREATE TABLE {table_fqn} "
            "(_org_id LowCardinality(String), payload String) "
            "ENGINE = MergeTree() ORDER BY _org_id"
        )
        ch_client.command(
            f"INSERT INTO {table_fqn} (_org_id, payload) VALUES "
            f"('{org_a}','a1'),('{org_a}','a2'),('{org_a}','a3'),"
            f"('{org_b}','b1'),('{org_b}','b2')"
        )
        ch_client.command("CREATE DATABASE IF NOT EXISTS dfe")
        ch_client.command(f"CREATE TABLE {otel_table} (x UInt8) ENGINE = MergeTree() ORDER BY x")
        ch_client.command(f"INSERT INTO {otel_table} VALUES (1)")

        secrets_dir = tmp_path_factory.mktemp("ch-secrets")
        store = build_secrets(SecretsSettings(provider="file", path=str(secrets_dir)))
        bindings = derive_group_bindings(groups, orgs)
        otel_role = [r for r in DEFAULT_SERVICE_ROLES if r.name == "otel_reader"]
        result = ChRbacReconciler(ch_client, secrets_store=store).reconcile(
            tiers=[tier], service_roles=otel_role, orgs=orgs, bindings=bindings
        )
        assert not result.errors, f"reconcile emitted errors: {result.errors}"

        yield {
            "params": ch_params,
            "table_fqn": table_fqn,
            "otel_table": otel_table,
            "store": store,
            "org_a": org_a,
            "org_b": org_b,
            "g_scoped": g_scoped,
            "g_open": g_open,
            "g_analyst": g_analyst,
            "g_admin": g_admin,
        }
    finally:
        _drop_all()


def _org_password(store, org: str) -> str:
    """The plaintext the reconciler minted for an org's CH user."""
    return store.get(f"ch/orgs/{org}")


def _group_password(store, group: str) -> str:
    return store.get(f"ch/groups/{group}")


class TestReconcilerPinsTheTenant:
    def test_each_org_user_sees_only_its_org(self, reconciled_world):
        """The whole point: a reconcile alone yields per-org isolated identities.

        Nothing here creates a user, grants a role or pins a setting - the
        reconciler did all three from the org registry.
        """
        w = reconciled_world
        a = count_as(
            w["params"],
            org_user_name(w["org_a"]),
            _org_password(w["store"], w["org_a"]),
            w["table_fqn"],
        )
        b = count_as(
            w["params"],
            org_user_name(w["org_b"]),
            _org_password(w["store"], w["org_b"]),
            w["table_fqn"],
        )
        assert (a, b) == (3, 2)

    def test_customer_group_is_pinned(self, reconciled_world):
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_scoped']}",
            _group_password(w["store"], w["g_scoped"]),
            w["table_fqn"],
        )
        assert n == 3

    def test_unrestricted_group_sees_all_rows(self, reconciled_world):
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_open']}",
            _group_password(w["store"], w["g_open"]),
            w["table_fqn"],
        )
        assert n == 5

    def test_platform_role_beats_org_markers(self, reconciled_world):
        """An analyst group matched by a domain rule still reads across orgs."""
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_analyst']}",
            _group_password(w["store"], w["g_analyst"]),
            w["table_fqn"],
        )
        assert n == 5

    def test_text_override_is_a_hard_error(self, reconciled_world):
        """The attack, against the reconciler's own minted identity.

        The analyst tier runs ``readonly=2`` so BI clients can set per-query
        output settings, which means the READONLY pin on the tenant setting is
        the wall that stops this (code 452). ``readonly=1`` (code 164) stays in
        the match for tiers that forbid all settings changes.
        """
        import clickhouse_connect

        w = reconciled_world
        client = clickhouse_connect.get_client(
            host=w["params"]["host"],
            port=w["params"]["port"],
            username=org_user_name(w["org_a"]),
            password=_org_password(w["store"], w["org_a"]),
            secure=w["params"]["secure"],
        )
        try:
            with pytest.raises(Exception, match=r"SETTING_CONSTRAINT_VIOLATION|452|READONLY|164"):
                client.query(
                    f"SELECT count() FROM {w['table_fqn']} "
                    f"SETTINGS SQL_current_tenant_id = '{w['org_b']}'"
                )
        finally:
            client.close()

    def test_benign_query_settings_stay_usable(self, reconciled_world):
        """BI clients send output settings with every query; the tier must not
        reject them (readonly=2, not 1) or every hyperdx UI query 500s."""
        import clickhouse_connect

        w = reconciled_world
        client = clickhouse_connect.get_client(
            host=w["params"]["host"],
            port=w["params"]["port"],
            username=org_user_name(w["org_a"]),
            password=_org_password(w["store"], w["org_a"]),
            secure=w["params"]["secure"],
        )
        try:
            n = client.query(
                f"SELECT count() FROM {w['table_fqn']} SETTINGS date_time_output_format = 'iso'"
            ).result_rows[0][0]
            assert n == 3
        finally:
            client.close()

    def test_admin_group_reads_otel(self, reconciled_world):
        """The composed otel_reader role: admins see platform telemetry."""
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_admin']}",
            _group_password(w["store"], w["g_admin"]),
            w["otel_table"],
        )
        assert n == 1

    def test_analyst_group_reads_otel(self, reconciled_world):
        """A platform analyst holds the broad dfe.* tier grant and no tenant role,
        so the otel deny policy never targets it - it reads platform telemetry."""
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_analyst']}",
            _group_password(w["store"], w["g_analyst"]),
            w["otel_table"],
        )
        assert n == 1

    def test_org_scoped_group_sees_no_otel(self, reconciled_world):
        """A fenced tenant group reaches dfe.* by grant (broad tier), but the
        `USING 0` deny policy on the non-_org_id otel table returns zero rows -
        isolation is the row policy, not grant-scoping."""
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_scoped']}",
            _group_password(w["store"], w["g_scoped"]),
            w["otel_table"],
        )
        assert n == 0

    def test_old_design_leftovers_are_swept(self, reconciled_world, ch_client):
        """Upgrade path: no per-org roles survive a reconcile."""
        rows = ch_client.query(
            "SELECT name FROM system.roles WHERE name LIKE 'dfe_org_%'"
        ).result_rows
        assert rows == []
        assert (
            ch_client.query(
                "SELECT count() FROM system.roles WHERE name = %(r)s",
                parameters={"r": TENANT_ROLE},
            ).result_rows[0][0]
            == 1
        )
