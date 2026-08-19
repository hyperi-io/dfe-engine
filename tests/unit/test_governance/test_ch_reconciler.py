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

import hashlib
from types import SimpleNamespace

from dfe_engine.governance.ch.models import ChServiceRole, ChTier, GroupChBinding
from dfe_engine.governance.ch.reconciler import (
    ChRbacReconciler,
    _default_tier_name,
    _tenant_granted_dbs,
    compute_drops,
)
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def _org(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(name=name, org_ids=ids)


class _FakeAdminClient:
    """Records executed DDL; every discovery query returns no rows.

    Enough to exercise the whole ``reconcile`` apply path without a cluster: the
    empty result_rows mean no existing objects to drop, so only the rendered
    create/alter/grant DDL is executed - which is exactly what we assert on.
    """

    def __init__(self) -> None:
        self.executed: list[str] = []

    def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(result_rows=[])

    def command(self, stmt: str) -> None:
        self.executed.append(stmt)


def _rec() -> ChRbacReconciler:
    # render_all is pure - it never touches the client.
    return ChRbacReconciler(admin_client=None)


# ── _hash_for (mint-or-reuse) ───────────────────────────────────────
# Real file-backed store, no mocks. Reuse is load-bearing: CREATE USER IF NOT
# EXISTS never rotates an existing password, so a fresh mint on every reconcile
# would leave ClickHouse holding a password the store no longer knows.


class TestHashFor:
    def _store(self, tmp_path):
        return build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))

    def test_mints_and_stores_plaintext_returning_its_hash(self, tmp_path):
        store = self._store(tmp_path)
        rec = ChRbacReconciler(admin_client=None, secrets_store=store)
        digest = rec._hash_for("ch/service/loader")
        # the plaintext is recoverable from the store; the hash is of that plaintext
        plaintext = store.get("ch/service/loader")
        assert digest == hashlib.sha256(plaintext.encode()).hexdigest()

    def test_reuses_existing_secret_across_runs(self, tmp_path):
        store = self._store(tmp_path)
        first = ChRbacReconciler(admin_client=None, secrets_store=store)._hash_for("ch/service/x")
        # a second reconciler (fresh instance, same store) must not rotate
        second = ChRbacReconciler(admin_client=None, secrets_store=store)._hash_for("ch/service/x")
        assert first == second

    def test_honours_a_preexisting_password(self, tmp_path):
        store = self._store(tmp_path)
        store.put("ch/service/loader", "already-set")
        rec = ChRbacReconciler(admin_client=None, secrets_store=store)
        assert rec._hash_for("ch/service/loader") == hashlib.sha256(b"already-set").hexdigest()

    def test_distinct_paths_get_distinct_secrets(self, tmp_path):
        store = self._store(tmp_path)
        rec = ChRbacReconciler(admin_client=None, secrets_store=store)
        assert rec._hash_for("ch/service/a") != rec._hash_for("ch/service/b")


# ── password <-> served-secret sync (the bug) ───────────────────────
# The pinned CH user authenticates with the password whose sha256 hash reconcile
# sets on it; /api/v1/hyperdx/connection serves the plaintext at the SAME secret
# path (ch/orgs/<org> for an org, ch/service/<name> for a service user). These
# assert the two agree AFTER a reconcile - so the hyperdx embed's connect can
# never hit CH auth 516 because CREATE USER IF NOT EXISTS left a stale password.


class TestReconcilePasswordSync:
    def _store(self, tmp_path):
        return build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))

    def _analyst_tiers(self):
        return [
            ChTier(name="analyst_tier_2", kind="analyst", default=True, grants=["SELECT ON dfe.*"])
        ]

    def test_org_user_password_matches_the_served_secret(self, tmp_path):
        store = self._store(tmp_path)
        client = _FakeAdminClient()
        ChRbacReconciler(client, secrets_store=store).reconcile(
            tiers=self._analyst_tiers(),
            service_roles=[],
            orgs=[_org("acme", ["acme"])],
            bindings=[],
        )
        # exactly the plaintext GET /hyperdx/connection reads for org 'acme'
        served = store.get("ch/orgs/acme")
        expected = hashlib.sha256(served.encode()).hexdigest()
        assert (
            f"ALTER USER `dfe_org_acme` IDENTIFIED WITH sha256_hash BY '{expected}'"
            in client.executed
        )

    def test_realigns_a_stale_preexisting_user_to_the_stored_secret(self, tmp_path):
        """The reproduced fault: the secret was rotated in the store while the CH
        user survived from an older epoch. CREATE USER IF NOT EXISTS is a no-op on
        it, so only the ALTER carries the realignment to the served value."""
        store = self._store(tmp_path)
        store.put("ch/orgs/acme", "rotated-pw")
        client = _FakeAdminClient()
        ChRbacReconciler(client, secrets_store=store).reconcile(
            tiers=self._analyst_tiers(),
            service_roles=[],
            orgs=[_org("acme", ["acme"])],
            bindings=[],
        )
        expected = hashlib.sha256(b"rotated-pw").hexdigest()
        assert (
            f"ALTER USER `dfe_org_acme` IDENTIFIED WITH sha256_hash BY '{expected}'"
            in client.executed
        )

    def test_service_user_password_matches_the_served_secret(self, tmp_path):
        store = self._store(tmp_path)
        client = _FakeAdminClient()
        ChRbacReconciler(client, secrets_store=store).reconcile(
            tiers=[],
            service_roles=[ChServiceRole(name="loader", mint_user=True, grants=["INSERT ON dfe.*"])],
            orgs=[],
            bindings=[],
        )
        served = store.get("ch/service/loader")
        expected = hashlib.sha256(served.encode()).hexdigest()
        assert (
            f"ALTER USER `dfe_loader` IDENTIFIED WITH sha256_hash BY '{expected}'"
            in client.executed
        )


class TestDefaultTierName:
    def test_flagged_default_wins(self):
        tiers = [
            ChTier(name="a1", kind="analyst"),
            ChTier(name="a2", kind="analyst", default=True),
        ]
        assert _default_tier_name(tiers, "analyst") == "a2"

    def test_first_of_kind_when_no_default(self):
        tiers = [ChTier(name="a1", kind="analyst"), ChTier(name="a2", kind="analyst")]
        assert _default_tier_name(tiers, "analyst") == "a1"

    def test_empty_when_kind_absent(self):
        assert _default_tier_name([ChTier(name="a1", kind="analyst")], "hunt") == ""


class TestTenantGrantedDbs:
    def test_extracts_db_wide_select_grants_from_analyst_tiers(self):
        tiers = [
            ChTier(
                name="a2",
                kind="analyst",
                grants=["SELECT ON dfe.*", "SELECT ON dfe_hunts.*"],
            ),
            ChTier(name="h2", kind="hunt", grants=["INSERT ON dfe_hunts.*"]),  # not analyst
        ]
        assert _tenant_granted_dbs(tiers) == ["dfe", "dfe_hunts"]

    def test_ignores_non_select_grants(self):
        tiers = [ChTier(name="a", kind="analyst", grants=["INSERT ON dfe.*"])]
        assert _tenant_granted_dbs(tiers) == []

    def test_default_analyst_tiers_discover_dfe(self):
        """The seeded analyst tiers grant broad dfe.* (D9), so dfe stays in the deny
        set, or the dfe.otel_* tables (no _org_id) would get no backstop policy."""
        from dfe_engine.governance.ch.models import DEFAULT_TIERS

        assert "dfe" in _tenant_granted_dbs(DEFAULT_TIERS)


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
        orgs = [_org("acme", ["acme"])]
        bindings = [
            GroupChBinding(group="soc", org="acme"),  # org-scoped, default tier
            GroupChBinding(group="admin"),  # unrestricted, default tier
            GroupChBinding(group="nohash"),  # no minted secret -> skipped
        ]
        return tiers, service_roles, orgs, bindings

    def test_full_composition_and_order(self):
        tiers, service_roles, orgs, bindings = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=service_roles,
            orgs=orgs,
            bindings=bindings,
            org_tables=[("dfe", "events")],
            service_hashes={"loader": "svchash"},
            group_hashes={"soc": "sochash", "admin": "adminhash"},  # 'nohash' absent
            org_hashes={"acme": "orghash"},
        )
        s = "\n".join(stmts)
        # ordering: tier role -> tenant axis -> org user -> group user
        assert s.index("dfe_analyst_tier_2_role") < s.index("dfe_tenant_role")
        assert s.index("dfe_rowpol_tenant_dfe_events") < s.index("dfe_org_acme")
        assert s.index("CREATE USER IF NOT EXISTS `dfe_org_acme`") < s.index("dfe_grp_soc")
        # minted service user rendered
        assert "CREATE USER IF NOT EXISTS `dfe_loader`" in s
        assert "GRANT `dfe_loader_role` TO `dfe_loader`" in s
        # the org's pinned user: tier + tenant role + READONLY pin
        assert "GRANT `dfe_tenant_role` TO `dfe_org_acme`" in s
        assert "ALTER USER `dfe_org_acme` SETTINGS SQL_current_tenant_id = 'acme' READONLY" in s
        # org-scoped group user: same pinned shape
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_grp_soc`" in s
        assert "ALTER USER `dfe_grp_soc` SETTINGS SQL_current_tenant_id = 'acme' READONLY" in s
        # unrestricted user: tier only, no tenant role, no pin
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_grp_admin`" in s
        assert "GRANT `dfe_tenant_role` TO `dfe_grp_admin`" not in s
        assert "ALTER USER `dfe_grp_admin` SETTINGS SQL_current_tenant_id" not in s
        # binding without a minted secret is skipped
        assert "dfe_grp_nohash" not in s

    def test_row_policy_isolation_end_to_end(self):
        """Full composition with the SEEDED tiers (D9): every user - org and
        platform alike - reads the whole data db via the broad analyst tier role, so
        a fenced org user sees every source table automatically. The org user is
        confined by the tenant role + pin plus the otel deny policy, NOT by a
        narrowed grant."""
        from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES, DEFAULT_TIERS

        orgs = [_org("acme", ["acme"])]
        bindings = [
            GroupChBinding(group="ops"),  # platform -> tier only, no pin
            GroupChBinding(group="soc", org="acme"),  # org-scoped -> fenced
        ]
        stmts = _rec().render_all(
            tiers=DEFAULT_TIERS,
            service_roles=DEFAULT_SERVICE_ROLES,
            orgs=orgs,
            bindings=bindings,
            org_tables=[("dfe", "default")],
            service_hashes={},
            group_hashes={"ops": "h1", "soc": "h2"},
            org_hashes={"acme": "h3"},
            deny_tables=[("dfe", "otel_logs")],
        )
        s = "\n".join(stmts)
        # the analyst tier role grants the whole data db - an org user reaches every
        # source table through it, never a narrowed dfe.default
        assert "GRANT SELECT ON dfe.* TO `dfe_analyst_tier_2_role`" in s
        assert "GRANT SELECT ON dfe.default TO `dfe_analyst_tier_2_role`" not in s
        # no per-user data grant: org and platform users alike only hold the tier
        # role, never a direct dfe.* grant on the user itself
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_org_acme`" in s
        assert "GRANT SELECT ON dfe.* TO `dfe_org_acme`" not in s
        assert "GRANT SELECT ON dfe.* TO `dfe_grp_ops`" not in s
        # only the org-scoped users are fenced by the tenant role + pin
        assert "GRANT `dfe_tenant_role` TO `dfe_org_acme`" in s
        assert "GRANT `dfe_tenant_role` TO `dfe_grp_soc`" in s
        assert "GRANT `dfe_tenant_role` TO `dfe_grp_ops`" not in s
        # the deny backstop fences dfe.otel_logs for the tenant role - the sole
        # control keeping otel away from a fenced user now the grant is broad
        assert (
            "`dfe_rowpol_tenant_dfe_otel_logs` ON `dfe`.`otel_logs` "
            "AS RESTRICTIVE FOR SELECT USING 0" in s
        )
        # dfe_query_reader (platform reader) keeps whole-db read incl dfe.otel_*
        assert "GRANT SELECT ON dfe.* TO `dfe_query_reader_role`" in s

    def test_explicit_tier_overrides_default(self):
        tiers, _service, _orgs, _b = self._inputs()
        b = [GroupChBinding(group="hunter", tier="hunt_tier_2")]
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=[],
            orgs=[],
            bindings=b,
            org_tables=[],
            service_hashes={},
            group_hashes={"hunter": "h"},
            org_hashes={},
        )
        assert "GRANT `dfe_hunt_tier_2_role` TO `dfe_grp_hunter`" in "\n".join(stmts)

    def test_binding_org_unknown_renders_unrestricted_group_user(self):
        """derive_group_bindings skips unknown orgs upstream; render_all's own
        guard degrades an unknown binding org to no pin rather than crashing."""
        tiers, _service, _orgs, _b = self._inputs()
        b = [GroupChBinding(group="x", org="ghost")]  # no Org 'ghost' exists
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=[],
            orgs=[],
            bindings=b,
            org_tables=[],
            service_hashes={},
            group_hashes={"x": "h"},
            org_hashes={},
        )
        s = "\n".join(stmts)
        assert "CREATE USER IF NOT EXISTS `dfe_grp_x`" in s
        assert "SQL_current_tenant_id" not in s

    def test_org_without_minted_secret_gets_no_user(self):
        tiers, _service, _orgs, _b = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=[],
            orgs=[_org("acme", ["acme"])],
            bindings=[],
            org_tables=[],
            service_hashes={},
            group_hashes={},
            org_hashes={},  # no secrets store
        )
        assert "dfe_org_acme" not in "\n".join(stmts)

    def test_non_mint_service_role_no_user(self):
        r = [ChServiceRole(name="query_reader", grants=["SELECT ON dfe.*"])]
        stmts = _rec().render_all(
            tiers=[],
            service_roles=r,
            orgs=[],
            bindings=[],
            org_tables=[],
            service_hashes={},
            group_hashes={},
            org_hashes={},
        )
        s = "\n".join(stmts)
        assert "CREATE ROLE IF NOT EXISTS `dfe_query_reader_role`" in s
        assert "CREATE USER" not in s

    def test_deny_tables_render_using_0_policies(self):
        tiers, _service, _orgs, _b = self._inputs()
        stmts = _rec().render_all(
            tiers=tiers,
            service_roles=[],
            orgs=[],
            bindings=[],
            org_tables=[("dfe", "default")],
            service_hashes={},
            group_hashes={},
            org_hashes={},
            deny_tables=[("dfe", "hunt_lease")],
        )
        s = "\n".join(stmts)
        assert "`dfe_rowpol_tenant_dfe_default` ON `dfe`.`default`" in s
        assert (
            "`dfe_rowpol_tenant_dfe_hunt_lease` ON `dfe`.`hunt_lease` "
            "AS RESTRICTIVE FOR SELECT USING 0" in s
        )


class TestComputeDrops:
    def test_old_design_leftovers_swept_policies_first(self):
        """Upgrade path: per-org roles and literal policies all drop."""
        existing_roles = {"dfe_org_acme_role", "dfe_org_gone_role"}
        existing_policies = [
            ("dfe_rowpol_acme_dfe_events", "dfe", "events"),
            ("dfe_rowpol_gone_dfe_events", "dfe", "events"),
        ]
        orgs = [_org("acme", ["acme"])]
        drops = compute_drops(existing_roles, existing_policies, set(), orgs, [("dfe", "events")])
        assert any("DROP ROW POLICY IF EXISTS `dfe_rowpol_acme_dfe_events`" in d for d in drops)
        assert any("DROP ROW POLICY IF EXISTS `dfe_rowpol_gone_dfe_events`" in d for d in drops)
        assert any("DROP ROLE IF EXISTS `dfe_org_acme_role`" in d for d in drops)
        assert any("DROP ROLE IF EXISTS `dfe_org_gone_role`" in d for d in drops)
        # policies drop before roles (a policy targets a role)
        pol_i = next(i for i, d in enumerate(drops) if "ROW POLICY" in d)
        role_i = next(i for i, d in enumerate(drops) if "DROP ROLE" in d)
        assert pol_i < role_i

    def test_no_drops_when_all_desired(self):
        existing_policies = [("dfe_rowpol_tenant_dfe_events", "dfe", "events")]
        existing_users = {"dfe_org_acme"}
        orgs = [_org("acme", ["acme"])]
        assert (
            compute_drops(set(), existing_policies, existing_users, orgs, [("dfe", "events")]) == []
        )

    def test_offboarded_org_user_dropped(self):
        """Deleting the org revokes the credential - the drop IS the offboarding."""
        existing_users = {"dfe_org_acme", "dfe_org_gone"}
        orgs = [_org("acme", ["acme"])]
        drops = compute_drops(set(), [], existing_users, orgs, [])
        assert drops == ["DROP USER IF EXISTS `dfe_org_gone`"]

    def test_ignores_non_dfe_objects(self):
        existing_roles = {"some_other_role", "dfe_org_gone_role"}
        existing_policies = [("handmade_policy", "dfe", "events")]
        existing_users = {"analyst_bob", "dfe_grp_soc"}
        drops = compute_drops(existing_roles, existing_policies, existing_users, [], [])
        assert all("some_other_role" not in d for d in drops)
        assert all("handmade_policy" not in d for d in drops)
        assert all("analyst_bob" not in d for d in drops)
        assert all("dfe_grp_soc" not in d for d in drops)
        assert any("dfe_org_gone_role" in d for d in drops)

    def test_deny_policies_are_desired_not_dropped(self):
        """A non-_org_id table's deny policy must survive the drop sweep."""
        existing_policies = [
            ("dfe_rowpol_tenant_dfe_default", "dfe", "default"),
            ("dfe_rowpol_tenant_dfe_hunt_lease", "dfe", "hunt_lease"),
        ]
        drops = compute_drops(
            set(),
            existing_policies,
            set(),
            [],
            [("dfe", "default")],
            [("dfe", "hunt_lease")],
        )
        assert drops == []
