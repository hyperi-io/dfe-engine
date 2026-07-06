#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_render.py
#  Purpose:      Pure CH-RBAC DDL rendering + config model unit tests (no cluster)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the pure config -> ClickHouse DDL rendering (spec section 7)."""

from __future__ import annotations

from types import SimpleNamespace

from dfe_engine.governance.ch.models import (
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    FIXED_USERS,
    TENANT_POLICY_NAME,
    TENANT_SETTING,
    ChServiceRole,
    ChTier,
)
from dfe_engine.governance.ch.render import (
    _sq,
    render_fixed_users,
    render_materialise,
    render_service_role,
    render_tenant_policies,
    render_tier,
)


def _joined(stmts: list[str]) -> str:
    return "\n".join(stmts)


class TestNaming:
    def test_tier_object_names(self):
        t = ChTier(name="analyst_tier_2", kind="analyst")
        assert t.role() == "dfe_analyst_tier_2_role"
        assert t.profile() == "dfe_analyst_tier_2_profile"
        assert t.quota_name() == "dfe_analyst_tier_2_quota"


class TestRenderTier:
    def test_analyst_tier_full(self):
        t = ChTier(
            name="analyst_tier_2",
            kind="analyst",
            grants=["SELECT ON dfe.*"],
            settings={"readonly": 1, "max_memory_usage": 4294967296, "max_execution_time": 300},
            quota={"interval": "1 hour", "queries": 1000, "errors": 100},
        )
        stmts = render_tier(t)
        s = _joined(stmts)
        # ROLE is created FIRST: it is the grantee of CREATE QUOTA ... TO role and the
        # target of ALTER ROLE ... SETTINGS PROFILE (F-RENDER-TIER-ORDER).
        assert stmts[0] == "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`"
        # profile + quota use OR REPLACE so an edited tier actually re-applies.
        prof_stmt = next(st for st in stmts if st.startswith("CREATE SETTINGS PROFILE"))
        assert prof_stmt.startswith(
            "CREATE SETTINGS PROFILE OR REPLACE `dfe_analyst_tier_2_profile`"
        )
        assert "readonly = 1" in prof_stmt
        assert "CREATE QUOTA OR REPLACE `dfe_analyst_tier_2_quota` FOR INTERVAL 1 hour MAX" in s
        assert "IF NOT EXISTS `dfe_analyst_tier_2_profile`" not in s
        assert "IF NOT EXISTS `dfe_analyst_tier_2_quota`" not in s
        assert "queries = 1000" in s
        assert "errors = 100" in s
        assert "interval = " not in s  # interval is not a per-interval maximum
        assert "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`" in s
        assert "GRANT SELECT ON dfe.* TO `dfe_analyst_tier_2_role`" in s
        assert (
            "ALTER ROLE `dfe_analyst_tier_2_role` SETTINGS PROFILE `dfe_analyst_tier_2_profile`"
            in s
        )
        # ordering invariants: role before quota grantee; profile before the ALTER
        assert s.index("CREATE ROLE") < s.index("CREATE QUOTA")
        assert s.index("CREATE SETTINGS PROFILE") < s.index("ALTER ROLE")
        # quota attaches to the tier ROLE, not a user
        assert "TO `dfe_analyst_tier_2_role`" in s

    def test_tier_without_quota_or_settings(self):
        stmts = render_tier(ChTier(name="bare", grants=["SELECT ON dfe.*"]))
        s = _joined(stmts)
        assert "CREATE SETTINGS PROFILE" not in s
        assert "CREATE QUOTA" not in s
        assert "ALTER ROLE" not in s
        assert "CREATE ROLE IF NOT EXISTS `dfe_bare_role`" in s

    def test_quota_maxima_excludes_interval(self):
        t = ChTier(name="x", quota={"interval": "1 day", "queries": 5})
        assert t.quota_maxima() == {"queries": 5}
        assert t.quota_interval() == "1 day"

    def test_role_created_before_quota_grantee(self):
        """CREATE ROLE must precede CREATE QUOTA ... TO role: on a fresh cluster a
        quota whose grantee role does not yet exist fails (F-RENDER-TIER-ORDER)."""
        t = ChTier(name="t", settings={"readonly": 1}, quota={"interval": "1 hour", "queries": 5})
        stmts = render_tier(t)
        role_i = next(i for i, x in enumerate(stmts) if x.startswith("CREATE ROLE"))
        quota_i = next(i for i, x in enumerate(stmts) if x.startswith("CREATE QUOTA"))
        prof_i = next(i for i, x in enumerate(stmts) if x.startswith("CREATE SETTINGS PROFILE"))
        alter_i = next(i for i, x in enumerate(stmts) if x.startswith("ALTER ROLE"))
        assert role_i < quota_i  # grantee exists before the quota targets it
        assert prof_i < alter_i  # profile exists before the ALTER attaches it

    def test_settings_profile_and_quota_use_or_replace(self):
        """OR REPLACE (not IF NOT EXISTS) so an EDITED tier's settings/quota actually
        re-apply on a cluster where the objects already exist (make-CH-match)."""
        t = ChTier(
            name="t",
            grants=["SELECT ON dfe.*"],
            settings={"readonly": 1},
            quota={"interval": "1 hour", "queries": 5},
        )
        s = _joined(render_tier(t))
        assert "CREATE SETTINGS PROFILE OR REPLACE `dfe_t_profile`" in s
        assert "CREATE QUOTA OR REPLACE `dfe_t_quota`" in s
        assert "IF NOT EXISTS `dfe_t_profile`" not in s
        assert "IF NOT EXISTS `dfe_t_quota`" not in s

    def test_rerender_same_tier_is_idempotent(self):
        """Re-rendering the SAME tier yields identical DDL: OR REPLACE run twice with
        the same body ends in the same state (no drift, no error)."""
        t = ChTier(
            name="t",
            grants=["SELECT ON dfe.*"],
            settings={"readonly": 1},
            quota={"interval": "1 hour", "queries": 5},
        )
        assert render_tier(t) == render_tier(t)


class TestRenderServiceRole:
    def test_loader(self):
        r = ChServiceRole(
            name="loader",
            mint_user=True,
            grants=["INSERT ON dfe.*"],
            settings={"async_insert": 1},
        )
        s = _joined(render_service_role(r))
        assert "CREATE SETTINGS PROFILE OR REPLACE `dfe_loader_profile`" in s
        assert "CREATE ROLE IF NOT EXISTS `dfe_loader_role`" in s
        assert "GRANT INSERT ON dfe.* TO `dfe_loader_role`" in s
        # the minted user is not rendered here (secret-bearing, reconciler's job)
        assert "CREATE USER" not in s
        assert r.user() == "dfe_loader"


class TestRenderTenantPolicies:
    """ONE RESTRICTIVE policy per _org_id table, driven by the custom setting."""

    def test_one_policy_per_table_shared_name_to_reader(self):
        stmts = render_tenant_policies([("dfe", "events"), ("dfe_hunts", "results")])
        s = _joined(stmts)
        assert stmts[0].startswith(
            "CREATE ROW POLICY OR REPLACE `dfe_tenant_filter` ON `dfe`.`events`"
        )
        # same short-name reused on every table (per-table scoped in CH)
        assert s.count(f"`{TENANT_POLICY_NAME}`") == 2
        assert "`dfe_hunts`.`results`" in s
        # always targets the single fixed reader
        assert s.count("TO `dfe_tenant_reader`") == 2

    def test_predicate_uses_has_splitbychar_getsetting(self):
        s = _joined(render_tenant_policies([("dfe", "events")]))
        assert "USING has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id)" in s
        # the setting name is the SSoT constant
        assert TENANT_SETTING in s

    def test_restrictive_only_never_permissive(self):
        # HARD CONSTRAINT (spec 5.2): a PERMISSIVE policy would flip the table to
        # default-deny for the un-targeted admin/analyst users.
        s = _joined(render_tenant_policies([("dfe", "events")]))
        assert "AS RESTRICTIVE FOR SELECT" in s
        assert "PERMISSIVE" not in s

    def test_or_replace_not_if_not_exists(self):
        # OR REPLACE so re-running re-applies the predicate (make-CH-match).
        s = _joined(render_tenant_policies([("dfe", "events")]))
        assert "CREATE ROW POLICY OR REPLACE" in s
        assert "ROW POLICY IF NOT EXISTS" not in s

    def test_no_tables_no_policies(self):
        assert render_tenant_policies([]) == []

    def test_rerender_is_idempotent(self):
        tables = [("dfe", "events"), ("dfe_hunts", "results")]
        assert render_tenant_policies(tables) == render_tenant_policies(tables)


class TestSqEscaping:
    """_sq must escape backslash BEFORE quote (F-ROWPOLICY-BACKSLASH)."""

    def test_backslash_doubled(self):
        assert _sq("a\\b") == "'a\\\\b'"

    def test_quote_doubled(self):
        assert _sq("o'brien") == "'o''brien'"

    def test_backslash_and_quote_ordered(self):
        # value = backslash + quote -> '\\' then '' : one bounded literal, no breakout
        assert _sq("\\'") == "'\\\\'''"

    def test_tenant_policy_setting_name_is_quoted_literal(self):
        # getSetting takes the setting NAME as a single-quoted string literal,
        # emitted through _sq so the predicate stays one bounded expression.
        s = _joined(render_tenant_policies([("dfe", "events")]))
        assert "getSetting('SQL_current_tenant_id')" in s


class TestRenderFixedUsers:
    """The fixed users by privilege (replaces per-group minting)."""

    def _render(self):
        # deterministic fake hashes keyed by fixed-user name
        return render_fixed_users({fu.name: f"h_{fu.name}" for fu in FIXED_USERS})

    def test_all_fixed_users_created_and_granted(self):
        s = _joined(self._render())
        assert "CREATE USER IF NOT EXISTS `dfe_analyst` " in s
        assert "CREATE USER IF NOT EXISTS `dfe_analyst_ro` " in s
        assert "CREATE USER IF NOT EXISTS `dfe_tenant_reader` " in s
        # dfe_admin is the CH superuser, never minted here
        assert "dfe_admin" not in s
        # grants go straight to the user (no intermediate role)
        assert "GRANT SELECT ON dfe.* TO `dfe_analyst_ro`" in s
        assert "GRANT INSERT ON dfe.* TO `dfe_analyst`" in s

    def test_hash_is_sha256_and_quoted(self):
        s = _joined(self._render())
        assert "IDENTIFIED WITH sha256_hash BY 'h_dfe_analyst'" in s

    def test_reader_is_readonly_with_changeable_tenant_setting(self):
        s = _joined(self._render())
        # the load-bearing reader clause: readonly + the ONE changeable setting.
        assert (
            "CREATE USER IF NOT EXISTS `dfe_tenant_reader` "
            "IDENTIFIED WITH sha256_hash BY 'h_dfe_tenant_reader' "
            "SETTINGS readonly = 1, SQL_current_tenant_id = '' CHANGEABLE_IN_READONLY" in s
        )
        # re-applied via ALTER USER so an existing user still gets the setting.
        assert (
            "ALTER USER `dfe_tenant_reader` SETTINGS readonly = 1, "
            "SQL_current_tenant_id = '' CHANGEABLE_IN_READONLY" in s
        )

    def test_read_write_analyst_has_no_readonly_or_tenant_setting(self):
        s = _joined(render_fixed_users({"dfe_analyst": "h"}))
        assert "readonly" not in s
        assert "CHANGEABLE_IN_READONLY" not in s
        assert "ALTER USER" not in s  # no settings -> no re-apply

    def test_user_without_hash_is_skipped(self):
        # No secrets store -> no hash -> no user (same discipline as service users).
        s = _joined(render_fixed_users({"dfe_tenant_reader": "h"}))
        assert "`dfe_tenant_reader`" in s
        assert "`dfe_analyst`" not in s

    def test_empty_hashes_render_nothing(self):
        assert render_fixed_users({}) == []


class TestDefaultTiers:
    def test_six_tiers_two_families(self):
        assert {t.name for t in DEFAULT_TIERS} == {
            "analyst_tier_1",
            "analyst_tier_2",
            "analyst_tier_3",
            "hunt_tier_1",
            "hunt_tier_2",
            "hunt_tier_3",
        }

    def test_one_default_per_kind(self):
        assert {t.kind: t.name for t in DEFAULT_TIERS if t.default} == {
            "analyst": "analyst_tier_2",
            "hunt": "hunt_tier_2",
        }

    def test_locked_memory_and_timeouts(self):
        by_name = {t.name: t for t in DEFAULT_TIERS}
        assert by_name["analyst_tier_1"].settings["max_memory_usage"] == 17179869184
        assert by_name["analyst_tier_1"].settings["max_execution_time"] == 600
        assert by_name["analyst_tier_3"].settings["max_execution_time"] == 2
        assert by_name["hunt_tier_2"].settings["max_execution_time"] == 120
        assert by_name["hunt_tier_3"].settings["max_memory_usage"] == 1073741824

    def test_analyst_readonly_hunt_not(self):
        by_name = {t.name: t for t in DEFAULT_TIERS}
        assert by_name["analyst_tier_2"].settings.get("readonly") == 1
        assert "readonly" not in by_name["hunt_tier_2"].settings

    def test_hunt_has_insert_grant_analyst_not(self):
        by_name = {t.name: t for t in DEFAULT_TIERS}
        assert any("INSERT" in g for g in by_name["hunt_tier_1"].grants)
        assert all("INSERT" not in g for g in by_name["analyst_tier_1"].grants)

    def test_every_default_tier_renders_profile_role_quota(self):
        for t in DEFAULT_TIERS:
            s = _joined(render_tier(t))
            assert f"CREATE ROLE IF NOT EXISTS `dfe_{t.name}_role`" in s
            assert f"CREATE SETTINGS PROFILE OR REPLACE `dfe_{t.name}_profile`" in s
            assert f"CREATE QUOTA OR REPLACE `dfe_{t.name}_quota`" in s


class TestDefaultServiceRoles:
    def test_three_service_roles(self):
        assert {r.name for r in DEFAULT_SERVICE_ROLES} == {"loader", "query_reader", "hunt_runner"}

    def test_mint_user_flags(self):
        by_name = {r.name: r for r in DEFAULT_SERVICE_ROLES}
        assert by_name["loader"].mint_user is True
        assert by_name["query_reader"].mint_user is True
        # hunt_runner is granted to hunt users alongside a tier, not its own user
        assert by_name["hunt_runner"].mint_user is False

    def test_query_reader_readonly_no_ddl_select_only(self):
        qr = {r.name: r for r in DEFAULT_SERVICE_ROLES}["query_reader"]
        assert qr.settings["readonly"] == 1
        assert qr.settings["allow_ddl"] == 0
        assert all("INSERT" not in g for g in qr.grants)

    def test_loader_inserts_and_async(self):
        loader = {r.name: r for r in DEFAULT_SERVICE_ROLES}["loader"]
        assert any("INSERT" in g for g in loader.grants)
        assert loader.settings["async_insert"] == 1

    def test_service_roles_render_a_role_each(self):
        for r in DEFAULT_SERVICE_ROLES:
            s = _joined(render_service_role(r))
            assert f"CREATE ROLE IF NOT EXISTS `dfe_{r.name}_role`" in s


class TestRenderMaterialise:
    def test_creates_meta_db_and_replacing_tables(self):
        s = _joined(render_materialise([], []))
        assert "CREATE DATABASE IF NOT EXISTS dfe_meta" in s
        assert "CREATE TABLE IF NOT EXISTS dfe_meta.orgs" in s
        assert "CREATE TABLE IF NOT EXISTS dfe_meta.ch_tiers" in s
        assert "ReplacingMergeTree(updated_at)" in s
        # full refresh each run so removed orgs/tiers drop out of the projection
        assert "TRUNCATE TABLE dfe_meta.orgs" in s
        assert "TRUNCATE TABLE dfe_meta.ch_tiers" in s

    def test_inserts_orgs_and_tiers(self):
        org = SimpleNamespace(
            name="acme", org_ids=["acme", "acme2"], display_name="Acme", enabled=True
        )
        tier = ChTier(name="analyst_tier_2", kind="analyst", default=True)
        s = _joined(render_materialise([org], [tier]))
        assert (
            "INSERT INTO dfe_meta.orgs (name, org_ids, display_name, enabled) VALUES "
            "('acme', ['acme', 'acme2'], 'Acme', 1)" in s
        )
        assert (
            "INSERT INTO dfe_meta.ch_tiers (name, kind, is_default) VALUES "
            "('analyst_tier_2', 'analyst', 1)" in s
        )

    def test_disabled_org_and_empty_ids(self):
        org = SimpleNamespace(name="x", org_ids=[], display_name="", enabled=False)
        s = _joined(render_materialise([org], []))
        assert "VALUES ('x', [], '', 0)" in s
