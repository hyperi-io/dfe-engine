#  Project:      dfe-engine
#  File:         tests/integration/test_governance/test_ch_group_users.py
#  Purpose:      Live-ClickHouse proof that a group's CH user lives and dies with the group
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A group's ClickHouse user, grants and secret follow the group, on a real ClickHouse.

Each test reconciles twice through the real path - group records ->
``derive_group_bindings`` -> ``reconcile`` - against a real ClickHouse and a real
file-backed secrets store, and reads what the second run left behind.

A reconcile is cluster-global: it drops ``dfe_grp_*`` users no binding renders. On
the harness's own per-test ClickHouse nothing else is there; on a shared cluster,
run this serially (``-n 0``), and the fixture refuses to run where it would drop
users it did not create.
"""

import uuid
from collections.abc import Iterator

import pytest

from dfe_engine.auth.groups import Group
from dfe_engine.governance.ch.bindings import derive_group_bindings
from dfe_engine.governance.ch.models import (
    TENANT_ROLE,
    ChServiceRole,
    ChTier,
    GroupChBinding,
    group_user_name,
    org_user_name,
)
from dfe_engine.governance.ch.reconciler import ChRbacReconciler, ReconcileResult
from dfe_engine.orgs.models import Org
from dfe_engine.schema.plan import render_one
from dfe_engine.secrets import DfeSecrets, build_secrets
from dfe_engine.settings import SecretsSettings

from .conftest import count_as, drop_safely

pytestmark = pytest.mark.integration

# The manifest objects the reconcile's dfe_meta projection writes into.
_META_IDS = ("db.meta", "meta.orgs", "meta.ch_tiers")


class GroupWorld:
    """A uid-scoped ClickHouse world the tests reconcile groups into, again and again."""

    def __init__(self, ch_client, ch_params: dict, store: DfeSecrets, uid: str) -> None:
        self.client = ch_client
        self.params = ch_params
        self.store = store
        self.uid = uid
        self.db = f"dfe_test_grp_{uid}"
        self.table_fqn = f"{self.db}.events"
        self.org_a = f"orga{uid}"
        self.org_b = f"orgb{uid}"
        self.orgs = [
            Org(name=self.org_a, org_ids=[self.org_a]),
            Org(name=self.org_b, org_ids=[self.org_b]),
        ]
        # Mirrors a seeded analyst tier, granting this world's own database.
        self.tier = ChTier(
            name=f"test{uid}",
            kind="analyst",
            default=True,
            grants=[f"SELECT ON {self.db}.*"],
            settings={"readonly": 2},
        )

    def group(self, base: str, *, roles: list[str], scope_org: str = "") -> Group:
        scope = f"org:{scope_org}" if scope_org else "system"
        return Group(name=f"{base}-{self.uid}", roles=roles, scope=scope)

    def reconcile(
        self, groups: list[Group], *, service_roles: list[ChServiceRole] | None = None
    ) -> ReconcileResult:
        bindings = derive_group_bindings(groups, self.orgs)
        result = ChRbacReconciler(self.client, secrets_store=self.store).reconcile(
            tiers=[self.tier],
            service_roles=service_roles or [],
            orgs=self.orgs,
            bindings=bindings,
        )
        assert not result.errors, f"reconcile emitted errors: {result.errors}"
        return result

    def users(self) -> set[str]:
        rows = self.client.query("SELECT name FROM system.users").result_rows
        return {r[0] for r in rows if self.uid in r[0]}

    def roles_held(self, user: str) -> set[str]:
        """The roles ClickHouse records as granted to ``user``."""
        rows = self.client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": user},
        ).result_rows
        return {r[0] for r in rows}

    def grants_held(self, user: str) -> int:
        """Role grants plus direct grants ClickHouse still records for ``user``."""
        params = {"u": user}
        roles = self.client.query(
            "SELECT count() FROM system.role_grants WHERE user_name = {u:String}",
            parameters=params,
        ).result_rows[0][0]
        direct = self.client.query(
            "SELECT count() FROM system.grants WHERE user_name = {u:String}",
            parameters=params,
        ).result_rows[0][0]
        return int(roles) + int(direct)

    def secret_exists(self, group: Group) -> bool:
        return self.store.exists(f"ch/groups/{group.name}")

    def rows_seen_by_group(self, group: Group) -> int:
        password = self.store.get(f"ch/groups/{group.name}")
        return count_as(self.params, group_user_name(group.name), password, self.table_fqn)

    def rows_seen_by_org(self, org: str) -> int:
        password = self.store.get(f"ch/orgs/{org}")
        return count_as(self.params, org_user_name(org), password, self.table_fqn)

    def teardown(self) -> None:
        for user in self.users():
            drop_safely(self.client, f"DROP USER IF EXISTS `{user}`")
        # The reconcile polices every _org_id table it discovers, cluster-wide. Safe
        # to sweep because the fixture refuses to run where foreign tenant users exist.
        try:
            existing = self.client.query(
                "SELECT short_name, database, table FROM system.row_policies "
                "WHERE short_name LIKE 'dfe_rowpol_tenant_%'"
            ).result_rows
        except Exception:
            existing = []
        for short_name, pdb, ptable in existing:
            drop_safely(
                self.client, f"DROP ROW POLICY IF EXISTS `{short_name}` ON `{pdb}`.`{ptable}`"
            )
        drop_safely(self.client, f"DROP ROLE IF EXISTS {self.tier.role()}")
        drop_safely(self.client, f"DROP SETTINGS PROFILE IF EXISTS {self.tier.profile()}")
        drop_safely(self.client, f"DROP DATABASE IF EXISTS {self.db}")


@pytest.fixture
def group_world(ch_client, ch_params, tmp_path) -> Iterator[GroupWorld]:
    """A world with two orgs' rows in one table and no group users yet."""
    try:
        ch_client.command("SET SQL_current_tenant_id = 'probe'")
    except Exception:
        pytest.skip("server does not allow the SQL_ custom-settings prefix")

    uid = uuid.uuid4().hex[:10]
    foreign = [
        r[0]
        for r in ch_client.query("SELECT name FROM system.users").result_rows
        if r[0].startswith(("dfe_org_", "dfe_grp_")) and uid not in r[0]
    ]
    if foreign:
        pytest.skip(
            f"refusing to reconcile: cluster carries tenant users we did not create: {foreign}"
        )

    secrets_dir = tmp_path / "ch-secrets"
    secrets_dir.mkdir()
    store = build_secrets(SecretsSettings(provider="file", path=str(secrets_dir)))
    world = GroupWorld(ch_client, ch_params, store, uid)
    try:
        for object_id in _META_IDS:
            for statement in render_one(object_id).statements:
                ch_client.command(statement)
        ch_client.command(f"CREATE DATABASE IF NOT EXISTS {world.db}")
        ch_client.command(
            f"CREATE TABLE {world.table_fqn} "
            "(_org_id LowCardinality(String), payload String) "
            "ENGINE = MergeTree() ORDER BY _org_id"
        )
        ch_client.command(
            f"INSERT INTO {world.table_fqn} (_org_id, payload) VALUES "
            f"('{world.org_a}','a1'),('{world.org_a}','a2'),('{world.org_a}','a3'),"
            f"('{world.org_b}','b1'),('{world.org_b}','b2')"
        )
        yield world
    finally:
        world.teardown()


class TestRolelessGroups:
    def test_a_roleless_group_gets_no_user(self, group_world):
        """Every OIDC-synced group starts roleless, and the old default was unrestricted."""
        w = group_world
        idle = w.group("idle", roles=[])
        w.reconcile([idle])
        assert group_user_name(idle.name) not in w.users()
        assert not w.secret_exists(idle)

    def test_a_group_that_loses_its_roles_loses_its_user(self, group_world):
        w = group_world
        ops = w.group("ops", roles=["data_analyst"])
        w.reconcile([ops])
        assert group_user_name(ops.name) in w.users()

        w.reconcile([ops.model_copy(update={"roles": []})])
        assert group_user_name(ops.name) not in w.users()
        assert not w.secret_exists(ops)

    def test_a_roleless_org_scoped_group_keeps_its_pinned_user(self, group_world):
        """Its scope is the org fence, so it reads that org's rows and nothing more."""
        w = group_world
        tenant = w.group("tenant", roles=[], scope_org=w.org_a)
        w.reconcile([tenant])
        assert w.rows_seen_by_group(tenant) == 3


class TestGroupsWithNoPlatformRole:
    def test_an_org_viewer_group_claiming_no_org_gets_no_user(self, group_world):
        """``org_viewer`` reads only its holder's orgs, and this group names none."""
        w = group_world
        viewers = w.group("viewers", roles=["org_viewer"])
        w.reconcile([viewers])
        assert group_user_name(viewers.name) not in w.users()
        assert not w.secret_exists(viewers)

    def test_a_group_left_with_only_org_viewer_loses_its_user(self, group_world):
        w = group_world
        ops = w.group("ops", roles=["data_analyst", "org_viewer"])
        w.reconcile([ops])
        assert w.rows_seen_by_group(ops) == 5

        w.reconcile([ops.model_copy(update={"roles": ["org_viewer"]})])
        assert group_user_name(ops.name) not in w.users()
        assert not w.secret_exists(ops)

    def test_an_org_viewer_group_claiming_an_org_keeps_its_pinned_user(self, group_world):
        w = group_world
        viewers = w.group("viewers", roles=["org_viewer"])
        w.reconcile([viewers.model_copy(update={"org_ids": [w.org_b]})])
        assert w.rows_seen_by_group(viewers) == 2


class TestDeletedGroups:
    def test_a_deleted_groups_user_grants_and_secret_go(self, group_world):
        w = group_world
        keep = w.group("keep", roles=["data_analyst"])
        gone = w.group("gone", roles=["data_analyst"])
        w.reconcile([keep, gone])
        gone_user = group_user_name(gone.name)
        assert gone_user in w.users()
        assert w.grants_held(gone_user) > 0
        assert w.secret_exists(gone)

        w.reconcile([keep])
        assert gone_user not in w.users()
        assert w.grants_held(gone_user) == 0
        assert not w.secret_exists(gone)

    def test_a_renamed_group_leaves_no_stale_user(self, group_world):
        w = group_world
        old = w.group("old", roles=["data_viewer"])
        new = w.group("new", roles=["data_viewer"])
        w.reconcile([old])
        w.reconcile([new])
        assert group_user_name(old.name) not in w.users()
        assert not w.secret_exists(old)
        assert w.rows_seen_by_group(new) == 5


@pytest.fixture
def otel_reader(group_world) -> Iterator[ChServiceRole]:
    """The otel_reader service role over the world's database, dropped after unless it pre-existed."""
    role = ChServiceRole(name="otel_reader", grants=[f"SELECT ON {group_world.db}.*"])
    existed = bool(
        group_world.client.query(
            "SELECT count() FROM system.roles WHERE name = {r:String}",
            parameters={"r": role.role()},
        ).result_rows[0][0]
    )
    yield role
    if not existed:
        drop_safely(group_world.client, f"DROP ROLE IF EXISTS `{role.role()}`")


class TestChangedGroups:
    def test_a_demoted_admin_group_loses_the_otel_reader_role(self, group_world, otel_reader):
        """Dropping admin removes what admin composed onto the user, not just the binding."""
        w = group_world
        ops = w.group("ops", roles=["admin"])
        w.reconcile([ops], service_roles=[otel_reader])
        user = group_user_name(ops.name)
        assert otel_reader.role() in w.roles_held(user)

        w.reconcile(
            [ops.model_copy(update={"roles": ["data_analyst"]})], service_roles=[otel_reader]
        )
        assert w.roles_held(user) == {w.tier.role()}

    def test_a_demotion_naming_a_missing_role_still_revokes_the_old_one(
        self, group_world, otel_reader
    ):
        """A replacing GRANT naming a role that does not exist fails whole, keeping the old set."""
        w = group_world
        group = f"ops-{w.uid}"
        reconciler = ChRbacReconciler(w.client, secrets_store=w.store)

        def reconcile(ch_roles: list[str], service_roles: list[ChServiceRole]) -> ReconcileResult:
            return reconciler.reconcile(
                tiers=[w.tier],
                service_roles=service_roles,
                orgs=w.orgs,
                bindings=[GroupChBinding(group=group, ch_roles=ch_roles)],
            )

        first = reconcile(["otel_reader"], [otel_reader])
        assert not first.errors, first.errors
        user = group_user_name(group)
        assert otel_reader.role() in w.roles_held(user)

        demoted = reconcile(["ghost"], [])
        assert w.roles_held(user) == {w.tier.role()}
        assert any("dfe_ghost_role" in error for error in demoted.errors), demoted.errors

    def test_a_group_that_turns_platform_wide_loses_its_tenant_fence(self, group_world):
        """A group gaining a platform role reads unrestricted, so the pin's role has to go."""
        w = group_world
        viewers = w.group("viewers", roles=["org_viewer"])
        pinned = viewers.model_copy(update={"org_ids": [w.org_b]})
        w.reconcile([pinned])
        assert w.rows_seen_by_group(viewers) == 2

        w.reconcile([pinned.model_copy(update={"roles": ["org_viewer", "data_analyst"]})])
        assert TENANT_ROLE not in w.roles_held(group_user_name(viewers.name))
        assert w.rows_seen_by_group(viewers) == 5


class TestOtherIdentitiesAreUntouched:
    def test_org_and_group_users_keep_working_with_the_same_passwords(self, group_world):
        w = group_world
        scoped = w.group("scoped", roles=["org_viewer"], scope_org=w.org_a)
        analyst = w.group("analyst", roles=["data_analyst"])
        gone = w.group("gone", roles=["data_analyst"])
        idle = w.group("idle", roles=[])
        w.reconcile([scoped, analyst, gone, idle])
        paths = [f"ch/orgs/{w.org_a}", f"ch/orgs/{w.org_b}"] + [
            f"ch/groups/{g.name}" for g in (scoped, analyst)
        ]
        before = {path: w.store.get(path) for path in paths}

        w.reconcile([scoped, analyst])
        assert {path: w.store.get(path) for path in paths} == before
        assert w.rows_seen_by_org(w.org_a) == 3
        assert w.rows_seen_by_org(w.org_b) == 2
        assert w.rows_seen_by_group(scoped) == 3
        assert w.rows_seen_by_group(analyst) == 5
