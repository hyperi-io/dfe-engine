#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_reconciler.py
#  Purpose:      Pure reconciler tests - render_all composition + stale-drop diff
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the pure parts of ChRbacReconciler (no cluster).

The apply path (execute + discovery against real CH) is covered by the real-CH
integration tests; here we prove the ordered DDL composition and the stale-object
diff, both of which are pure functions.
"""

from __future__ import annotations

from types import SimpleNamespace

from dfe_engine.governance.ch.models import FIXED_USERS, ChServiceRole, ChTier
from dfe_engine.governance.ch.reconciler import (
    ChRbacReconciler,
    compute_drops,
    load_catalogue_from_gitcrud,
    reconcile_ch_rbac,
)


class _FakeCH:
    """A minimal duck-typed ClickHouse admin client for reconcile() unit tests.

    Task-sanctioned fake (NO live CH): ``query`` answers the three discovery reads
    from canned rows; ``command`` records every executed statement so a test can
    assert what the reconciler DID (or did NOT) emit.
    """

    def __init__(
        self,
        *,
        columns: list[tuple[str, str]] | None = None,
        roles: list[str] | None = None,
        policies: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self._columns = columns or []
        self._roles = roles or []
        self._policies = policies or []
        self.commands: list[str] = []

    def query(self, sql: str, parameters: dict | None = None):
        if "system.columns" in sql:
            rows: list = list(self._columns)
        elif "system.roles" in sql:
            rows = [(r,) for r in self._roles]
        elif "system.row_policies" in sql:
            rows = list(self._policies)
        else:
            rows = []
        return SimpleNamespace(result_rows=rows)

    def command(self, stmt: str) -> None:
        self.commands.append(stmt)


class _FakeSecrets:
    """In-memory scalo.secrets seam - lets reconcile() mint deterministic hashes."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def exists(self, path: str) -> bool:
        return path in self._store

    def get(self, path: str) -> str:
        return self._store[path]

    def put(self, path: str, value: str) -> None:
        self._store[path] = value


def _analyst_default() -> ChTier:
    return ChTier(name="analyst_tier_2", kind="analyst", default=True, grants=["SELECT ON dfe.*"])


def _org(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids)


def _rec() -> ChRbacReconciler:
    # render_all is pure - it never touches the client.
    return ChRbacReconciler(admin_client=None)


class TestRenderAll:
    def _inputs(self):
        tiers = [
            ChTier(
                name="analyst_tier_2",
                kind="analyst",
                default=True,
                grants=["SELECT ON dfe.*"],
                settings={"readonly": 1},
            ),
            ChTier(name="hunt_tier_2", kind="hunt", default=True, grants=["INSERT ON dfe_hunts.*"]),
        ]
        service_roles = [ChServiceRole(name="loader", mint_user=True, grants=["INSERT ON dfe.*"])]
        return tiers, service_roles

    def _fixed_hashes(self):
        return {fu.name: f"h_{fu.name}" for fu in FIXED_USERS}

    def test_full_composition_and_order(self):
        tiers, service_roles = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=service_roles,
            org_tables=[("dfe", "events")],
            service_hashes={"loader": "svchash"},
            fixed_hashes=self._fixed_hashes(),
        )
        s = "\n".join(stmts)
        # ordering: tier role -> service user -> fixed user -> tenant policy
        assert s.index("dfe_analyst_tier_2_role") < s.index("`dfe_loader`")
        assert s.index("`dfe_tenant_reader`") < s.index("CREATE ROW POLICY OR REPLACE")
        # minted service user rendered
        assert "CREATE USER IF NOT EXISTS `dfe_loader`" in s
        assert "GRANT `dfe_loader_role` TO `dfe_loader`" in s
        # fixed users minted (grants straight to the user)
        assert "CREATE USER IF NOT EXISTS `dfe_analyst`" in s
        assert "CREATE USER IF NOT EXISTS `dfe_tenant_reader`" in s
        # ONE tenant policy on the discovered _org_id table, to the reader
        assert (
            "CREATE ROW POLICY OR REPLACE `dfe_tenant_filter` ON `dfe`.`events` "
            "AS RESTRICTIVE FOR SELECT USING "
            "has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id) "
            "TO `dfe_tenant_reader`" in s
        )
        # NO retired per-org / per-group objects survive the model change
        assert "dfe_org_" not in s
        assert "dfe_grp_" not in s

    def test_no_secrets_renders_roles_and_policy_but_no_users(self):
        # Empty hashes (no secrets store) -> tiers + roles + tenant policy, no users.
        tiers, service_roles = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=service_roles,
            org_tables=[("dfe", "events")],
            service_hashes={},
            fixed_hashes={},
        )
        s = "\n".join(stmts)
        assert "CREATE ROLE IF NOT EXISTS `dfe_loader_role`" in s  # role still rendered
        assert "CREATE USER" not in s  # nothing minted
        assert "CREATE ROW POLICY OR REPLACE `dfe_tenant_filter`" in s  # policy still rendered

    def test_no_org_tables_no_policies(self):
        tiers, service_roles = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=service_roles,
            org_tables=[],
            service_hashes={},
            fixed_hashes=self._fixed_hashes(),
        )
        s = "\n".join(stmts)
        assert "CREATE ROW POLICY" not in s
        assert "CREATE USER IF NOT EXISTS `dfe_tenant_reader`" in s  # users still rendered

    def test_non_mint_service_role_no_user(self):
        r = [ChServiceRole(name="query_reader", grants=["SELECT ON dfe.*"])]
        stmts = _rec().render_all(
            tiers=[],
            service_roles=r,
            org_tables=[],
            service_hashes={},
            fixed_hashes={},
        )
        s = "\n".join(stmts)
        assert "CREATE ROLE IF NOT EXISTS `dfe_query_reader_role`" in s
        assert "CREATE USER" not in s


class TestComputeDrops:
    def test_retired_per_org_objects_dropped_policies_first(self):
        # The fixed-user model desires NO per-org objects, so every legacy
        # dfe_rowpol_* policy + dfe_org_* role is dropped wholesale.
        existing_roles = {"dfe_org_acme_role", "dfe_org_gone_role", "keep_me"}
        existing_policies = [
            ("dfe_rowpol_acme_dfe_events", "dfe", "events"),
            ("dfe_rowpol_gone_dfe_events", "dfe", "events"),
        ]
        drops = compute_drops(existing_roles, existing_policies, [("dfe", "events")])
        assert any("DROP ROW POLICY IF EXISTS `dfe_rowpol_acme_dfe_events`" in d for d in drops)
        assert any("DROP ROW POLICY IF EXISTS `dfe_rowpol_gone_dfe_events`" in d for d in drops)
        assert any("DROP ROLE IF EXISTS `dfe_org_acme_role`" in d for d in drops)
        assert any("DROP ROLE IF EXISTS `dfe_org_gone_role`" in d for d in drops)
        assert all("keep_me" not in d for d in drops)  # non-dfe role untouched
        # policies drop before roles (a policy targets a role)
        pol_i = next(i for i, d in enumerate(drops) if "ROW POLICY" in d)
        role_i = next(i for i, d in enumerate(drops) if "DROP ROLE" in d)
        assert pol_i < role_i

    def test_stale_tenant_filter_dropped_off_non_org_table_only(self):
        # dfe_tenant_filter on a table that no longer carries _org_id is dropped;
        # the one on a live _org_id table is kept.
        existing_policies = [
            ("dfe_tenant_filter", "dfe", "events"),  # still an _org_id table -> keep
            ("dfe_tenant_filter", "dfe", "legacy"),  # no longer -> drop
        ]
        drops = compute_drops(set(), existing_policies, [("dfe", "events")])
        assert any("`dfe_tenant_filter` ON `dfe`.`legacy`" in d for d in drops)
        assert all("`dfe`.`events`" not in d for d in drops)  # live table untouched

    def test_no_drops_when_clean(self):
        # Only the desired tenant policy on a live table, no legacy objects.
        drops = compute_drops(set(), [("dfe_tenant_filter", "dfe", "events")], [("dfe", "events")])
        assert drops == []

    def test_ignores_handmade_objects(self):
        drops = compute_drops({"some_other_role"}, [("handmade_policy", "dfe", "events")], [])
        assert drops == []


class TestReconcile:
    """reconcile() mints the fixed users + applies ONE tenant policy per discovered
    _org_id table, and cleans up the retired per-org objects."""

    def test_mints_fixed_users_and_applies_tenant_policy(self):
        ch = _FakeCH(columns=[("dfe", "events")])
        result = ChRbacReconciler(ch, secrets_store=_FakeSecrets()).reconcile(
            tiers=[_analyst_default()],
            service_roles=[],
            orgs=[_org("acme", ["acme"])],
        )
        applied = "\n".join(ch.commands)
        assert "CREATE USER IF NOT EXISTS `dfe_tenant_reader`" in applied
        assert "CREATE USER IF NOT EXISTS `dfe_analyst`" in applied
        # secret minted per fixed user at ch/fixed/<user>
        assert all(f"fixed/{fu.name}" in result.minted for fu in FIXED_USERS)
        assert "CREATE ROW POLICY OR REPLACE `dfe_tenant_filter` ON `dfe`.`events`" in applied
        assert result.errors == []

    def test_no_secrets_store_skips_users_keeps_policy(self):
        ch = _FakeCH(columns=[("dfe", "events")])
        result = ChRbacReconciler(ch).reconcile(  # no secrets_store
            tiers=[_analyst_default()],
            service_roles=[],
            orgs=[],
        )
        applied = "\n".join(ch.commands)
        assert "CREATE USER" not in applied
        assert result.minted == []
        assert "CREATE ROW POLICY OR REPLACE `dfe_tenant_filter`" in applied

    def test_drops_retired_per_org_objects(self):
        ch = _FakeCH(
            columns=[("dfe", "events")],
            roles=["dfe_org_gone_role"],
            policies=[("dfe_rowpol_gone_dfe_events", "dfe", "events")],
        )
        result = ChRbacReconciler(ch, secrets_store=_FakeSecrets()).reconcile(
            tiers=[_analyst_default()],
            service_roles=[],
            orgs=[],
        )
        applied = "\n".join(ch.commands)
        assert "DROP ROLE IF EXISTS `dfe_org_gone_role`" in applied
        assert "DROP ROW POLICY IF EXISTS `dfe_rowpol_gone_dfe_events`" in applied
        assert any("dfe_org_gone_role" in d for d in result.dropped)


class TestCatalogueFromGitcrud:
    """The e join: load ch_tiers / ch_service_roles from gitcrud, else fall back to
    the seeded DEFAULT_* - proven end to end through reconcile_ch_rbac."""

    def _crud(self, tmp_path):
        # Real local gitops repo (dulwich, no network) + the default registry which
        # already registers ch_tiers/ch_service_roles as versioned classes.
        from dfe_engine.gitcrud import GitCrud, VersionedDoc, default_registry
        from dfe_engine.gitops.repo import GitopsRepo

        repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
        crud = GitCrud(repo, default_registry())
        return crud, VersionedDoc(crud)

    def test_gitcrud_tiers_override_seeds(self, tmp_path):
        crud, vdoc = self._crud(tmp_path)
        vdoc.save_draft(
            "ch_tiers",
            "custom_tier",
            {
                "name": "custom_tier",
                "kind": "analyst",
                "default": True,
                "grants": ["SELECT ON dfe.*"],
            },
            actor="kaz",
        )
        vdoc.publish("ch_tiers", "custom_tier", actor="kaz")

        tiers, service_roles = load_catalogue_from_gitcrud(crud)
        assert [t.name for t in tiers] == ["custom_tier"]
        assert service_roles == []  # none published -> empty (caller keeps the seeds)

        ch = _FakeCH(columns=[("dfe", "events")])
        result = reconcile_ch_rbac(ch, orgs=[], gitcrud=crud)
        applied = "\n".join(result.statements)
        assert "CREATE ROLE IF NOT EXISTS `dfe_custom_tier_role`" in applied
        assert "dfe_analyst_tier_1_role" not in applied  # seeds NOT used for tiers
        # service roles had none published -> seeds still apply
        assert "CREATE ROLE IF NOT EXISTS `dfe_loader_role`" in applied

    def test_no_gitcrud_uses_seeds(self):
        ch = _FakeCH(columns=[("dfe", "events")])
        result = reconcile_ch_rbac(ch, orgs=[])  # no gitcrud handle
        applied = "\n".join(result.statements)
        assert "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`" in applied
        assert "CREATE ROLE IF NOT EXISTS `dfe_loader_role`" in applied

    def test_empty_gitcrud_falls_back_to_seeds(self, tmp_path):
        crud, _vdoc = self._crud(tmp_path)  # nothing published
        tiers, service_roles = load_catalogue_from_gitcrud(crud)
        assert tiers == []
        assert service_roles == []
        ch = _FakeCH(columns=[("dfe", "events")])
        result = reconcile_ch_rbac(ch, orgs=[], gitcrud=crud)
        assert "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`" in "\n".join(result.statements)
