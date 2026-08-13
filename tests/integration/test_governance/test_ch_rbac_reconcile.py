"""Live-ClickHouse proof that a reconcile alone isolates an org's identity.

`test_ch_rbac_isolation.py` builds its DDL by hand, so it would pass even if the
engine rendered nothing. This drives the real path instead - org registry + group
store -> `derive_group_bindings` -> `reconcile` - and connects as the users the
reconciler itself minted.

RUN THIS SERIALLY (`-n 0`). A reconcile is cluster-global: it sweeps stale
`dfe_org_*` users and `dfe_rowpol_*` policies, so two concurrent workers delete
each other's objects. The fixture refuses to run where that would destroy
objects it did not create.

No mocks (project policy): a real CH, a real file-backed secrets store.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from dfe_engine.governance.ch.models import TENANT_ROLE, org_user_name

from .conftest import count_as, drop_safely

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def reconciled_world(admin_client, conn_params, tmp_path_factory):
    """Reconcile a uid-scoped world through the real entry point.

    Every object is uid-suffixed and dropped in teardown. The reconcile also
    rebuilds the ``dfe_meta`` projection, which is ordinary reconcile behaviour
    (gitops stays the source of truth), so it is left in place.
    """
    from dfe_engine.governance.ch.bindings import derive_group_bindings
    from dfe_engine.governance.ch.models import ChTier
    from dfe_engine.governance.ch.reconciler import ChRbacReconciler
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import SecretsSettings

    ch_client = admin_client
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

    orgs = [
        SimpleNamespace(name=org_a, org_ids=[org_a]),
        SimpleNamespace(name=org_b, org_ids=[org_b]),
    ]
    groups = [
        SimpleNamespace(name=g_scoped, scope_org=org_a, org_ids=[], roles=["customer_viewer"]),
        SimpleNamespace(name=g_open, scope_org="", org_ids=[], roles=[]),
        SimpleNamespace(name=g_analyst, scope_org="", org_ids=[org_a], roles=["data_analyst"]),
    ]
    # A uid-scoped tier so the users can read the test table; the seeded tiers
    # grant SELECT on dfe.* only, and their names are not uid-scoped.
    tier = ChTier(
        name=f"test{uid}",
        kind="analyst",
        default=True,
        grants=[f"SELECT ON {db}.*"],
        settings={"readonly": 1},
        quota={"interval": "1 hour", "queries": 1000},
    )
    users = [org_user_name(org_a), org_user_name(org_b)] + [
        f"dfe_grp_{g}" for g in (g_scoped, g_open, g_analyst)
    ]

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

    try:
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

        secrets_dir = tmp_path_factory.mktemp("ch-secrets")
        store = build_secrets(SecretsSettings(provider="file", path=str(secrets_dir)))
        bindings = derive_group_bindings(groups, orgs)
        result = ChRbacReconciler(ch_client, secrets_store=store).reconcile(
            tiers=[tier], service_roles=[], orgs=orgs, bindings=bindings
        )
        assert not result.errors, f"reconcile emitted errors: {result.errors}"

        yield {
            "params": conn_params,
            "table_fqn": table_fqn,
            "store": store,
            "org_a": org_a,
            "org_b": org_b,
            "g_scoped": g_scoped,
            "g_open": g_open,
            "g_analyst": g_analyst,
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

        Two independent walls, either is fatal: the analyst tier's ``readonly=1``
        profile rejects any settings change (code 164), and the READONLY pin
        rejects this one specifically (code 452).
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

    def test_old_design_leftovers_are_swept(self, reconciled_world, admin_client):
        """Upgrade path: no per-org roles survive a reconcile."""
        rows = admin_client.query(
            "SELECT name FROM system.roles WHERE name LIKE 'dfe_org_%'"
        ).result_rows
        assert rows == []
        assert (
            admin_client.query(
                "SELECT count() FROM system.roles WHERE name = %(r)s",
                parameters={"r": TENANT_ROLE},
            ).result_rows[0][0]
            == 1
        )
