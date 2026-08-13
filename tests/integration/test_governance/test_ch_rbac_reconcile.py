"""Live-ClickHouse proof that a reconcile alone isolates an org-tied group.

`test_ch_rbac_isolation.py` builds its users and GRANTs by hand, so it would pass
even if the engine granted nothing. This drives the real path instead - group
store -> `derive_group_bindings` -> `reconcile` - and connects as the user the
reconciler itself minted.

RUN THIS SERIALLY (`-n 0`). A reconcile is cluster-global: it DROPS every
`dfe_org_*` role with no Org behind it, so two concurrent workers delete each
other's roles. The fixture refuses to run where that would destroy roles it did
not create.

No mocks (project policy): a real CH, a real file-backed secrets store.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from dfe_engine.governance.ch.models import org_role_name

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
    uid = uuid.uuid4().hex[:10]
    db = f"dfe_test_rec_{uid}"
    table = "events"
    table_fqn = f"{db}.{table}"
    org_a = f"orga{uid}"
    org_b = f"orgb{uid}"

    foreign = [
        r[0]
        for r in ch_client.query("SELECT name FROM system.roles").result_rows
        if r[0].startswith("dfe_org_") and uid not in r[0]
    ]
    if foreign:
        pytest.skip(
            f"refusing to reconcile: cluster carries org roles we did not create: {foreign}"
        )

    g_scoped = f"grp-scoped-{uid}"  # org-tied -> sees only org_a
    g_open = f"grp-open-{uid}"  # no org -> unrestricted
    g_multi = f"grp-multi-{uid}"  # two orgs -> inexpressible, gets no user

    orgs = [
        SimpleNamespace(name=org_a, org_ids=[org_a]),
        SimpleNamespace(name=org_b, org_ids=[org_b]),
    ]
    groups = [
        SimpleNamespace(name=g_scoped, scope_org=org_a, org_ids=[]),
        SimpleNamespace(name=g_open, scope_org="", org_ids=[]),
        SimpleNamespace(name=g_multi, scope_org="", org_ids=[org_a, org_b]),
    ]
    # A uid-scoped tier so the group users can read the test table; the seeded
    # tiers grant SELECT on dfe.* only, and their names are not uid-scoped.
    tier = ChTier(
        name=f"test{uid}",
        kind="analyst",
        default=True,
        grants=[f"SELECT ON {db}.*"],
        settings={"readonly": 1},
        quota={"interval": "1 hour", "queries": 1000},
    )
    users = [f"dfe_grp_{g}" for g in (g_scoped, g_open, g_multi)]

    def _drop_all() -> None:
        for u in users:
            drop_safely(ch_client, f"DROP USER IF EXISTS `{u}`")
        # The reconciler discovers _org_id tables cluster-wide, so it policies far
        # more than this test's own table; drop by what actually exists.
        try:
            existing = ch_client.query(
                "SELECT short_name, database, table FROM system.row_policies "
                "WHERE short_name LIKE %(a)s OR short_name LIKE %(b)s",
                parameters={"a": f"dfe_rowpol_{org_a}%", "b": f"dfe_rowpol_{org_b}%"},
            ).result_rows
        except Exception:
            existing = []
        for short_name, pdb, ptable in existing:
            drop_safely(
                ch_client, f"DROP ROW POLICY IF EXISTS `{short_name}` ON `{pdb}`.`{ptable}`"
            )
        for org in (org_a, org_b):
            drop_safely(ch_client, f"DROP ROLE IF EXISTS {org_role_name(org)}")
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
            "bindings": bindings,
            "g_scoped": g_scoped,
            "g_open": g_open,
            "g_multi": g_multi,
        }
    finally:
        _drop_all()


def _password_for(store, group: str) -> str:
    """The plaintext the reconciler minted for a group's CH user."""
    return store.get(f"ch/groups/{group}")


class TestReconcilerGrantsTheOrgRole:
    def test_org_tied_group_sees_only_its_org(self, reconciled_world):
        """The whole point: a reconcile alone restricts an org-tied group.

        Nothing here creates a user or writes a GRANT - the reconciler did both,
        from the group's scope.
        """
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_scoped']}",
            _password_for(w["store"], w["g_scoped"]),
            w["table_fqn"],
        )
        assert n == 3

    def test_group_with_no_org_is_unrestricted(self, reconciled_world):
        """The platform axis: tier-limited, but not row-filtered."""
        w = reconciled_world
        n = count_as(
            w["params"],
            f"dfe_grp_{w['g_open']}",
            _password_for(w["store"], w["g_open"]),
            w["table_fqn"],
        )
        assert n == 5

    def test_inexpressible_group_gets_no_user_at_all(self, reconciled_world, admin_client):
        """A multi-org group is skipped, not emitted unrestricted.

        Emitting it would hold no org role and therefore read every org's rows,
        so the absence of the user IS the isolation guarantee.
        """
        w = reconciled_world
        assert w["g_multi"] not in {b.group for b in w["bindings"]}
        rows = admin_client.query(
            "SELECT count() FROM system.users WHERE name = %(u)s",
            parameters={"u": f"dfe_grp_{w['g_multi']}"},
        ).result_rows
        assert rows[0][0] == 0
