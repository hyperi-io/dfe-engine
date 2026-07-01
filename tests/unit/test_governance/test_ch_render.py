#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_ch_render.py
#  Purpose:      Pure CH-RBAC DDL rendering + config model unit tests (no cluster)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the pure config -> ClickHouse DDL rendering (spec section 7)."""

from __future__ import annotations

from dfe_engine.governance.ch.models import (
    DEFAULT_TIERS,
    ChServiceRole,
    ChTier,
    GroupChBinding,
    org_policy_name,
    org_role_name,
)
from dfe_engine.governance.ch.render import (
    render_group_user,
    render_org_role,
    render_service_role,
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

    def test_org_names(self):
        assert org_role_name("acme") == "dfe_org_acme_role"
        assert org_policy_name("acme", "dfe", "events") == "dfe_rowpol_acme_dfe_events"

    def test_group_user_default_and_override(self):
        assert GroupChBinding(group="soc-ro").user() == "dfe_grp_soc-ro"
        assert GroupChBinding(group="x", ch_user="custom").user() == "custom"


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
        # profile is rendered before the role is altered to carry it
        assert stmts[0].startswith(
            "CREATE SETTINGS PROFILE IF NOT EXISTS `dfe_analyst_tier_2_profile`"
        )
        assert "readonly = 1" in stmts[0]
        assert "CREATE QUOTA IF NOT EXISTS `dfe_analyst_tier_2_quota` FOR INTERVAL 1 hour MAX" in s
        assert "queries = 1000" in s
        assert "errors = 100" in s
        assert "interval = " not in s  # interval is not a per-interval maximum
        assert "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`" in s
        assert "GRANT SELECT ON dfe.* TO `dfe_analyst_tier_2_role`" in s
        assert (
            "ALTER ROLE `dfe_analyst_tier_2_role` SETTINGS PROFILE `dfe_analyst_tier_2_profile`"
            in s
        )
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


class TestRenderServiceRole:
    def test_loader(self):
        r = ChServiceRole(
            name="loader",
            mint_user=True,
            grants=["INSERT ON dfe.*"],
            settings={"async_insert": 1},
        )
        s = _joined(render_service_role(r))
        assert "CREATE SETTINGS PROFILE IF NOT EXISTS `dfe_loader_profile`" in s
        assert "CREATE ROLE IF NOT EXISTS `dfe_loader_role`" in s
        assert "GRANT INSERT ON dfe.* TO `dfe_loader_role`" in s
        # the minted user is not rendered here (secret-bearing, reconciler's job)
        assert "CREATE USER" not in s
        assert r.user() == "dfe_loader"


class TestRenderOrgRole:
    def test_single_org_id_uses_equals(self):
        s = _joined(render_org_role("acme", ["acme"], [("dfe", "events")]))
        assert "CREATE ROLE IF NOT EXISTS `dfe_org_acme_role`" in s
        assert "AS RESTRICTIVE FOR SELECT USING _org_id = 'acme'" in s
        assert "`dfe_rowpol_acme_dfe_events` ON `dfe`.`events`" in s
        assert "PERMISSIVE" not in s  # HARD CONSTRAINT: restrictive-only

    def test_multi_org_id_uses_in(self):
        s = _joined(render_org_role("grp", ["a", "b"], [("dfe", "events")]))
        assert "USING _org_id IN ('a', 'b')" in s

    def test_policy_per_table(self):
        stmts = render_org_role("acme", ["acme"], [("dfe", "events"), ("dfe_hunts", "results")])
        s = _joined(stmts)
        assert s.count("CREATE ROW POLICY") == 2
        assert "`dfe_hunts`.`results`" in s

    def test_no_tables_just_role(self):
        stmts = render_org_role("acme", ["acme"], [])
        assert stmts == ["CREATE ROLE IF NOT EXISTS `dfe_org_acme_role`"]

    def test_org_id_escaping(self):
        s = _joined(render_org_role("x", ["o'brien"], [("dfe", "t")]))
        assert "_org_id = 'o''brien'" in s


class TestRenderGroupUser:
    def test_org_scoped_user(self):
        s = _joined(
            render_group_user(
                "dfe_grp_soc",
                "abc123",
                tier_role="dfe_analyst_tier_2_role",
                org_role="dfe_org_acme_role",
            )
        )
        assert (
            "CREATE USER IF NOT EXISTS `dfe_grp_soc` IDENTIFIED WITH sha256_hash BY 'abc123'" in s
        )
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_grp_soc`" in s
        assert "GRANT `dfe_org_acme_role` TO `dfe_grp_soc`" in s

    def test_unrestricted_user_no_org_grant(self):
        s = _joined(render_group_user("dfe_grp_admin", "h", tier_role="dfe_analyst_tier_1_role"))
        assert "GRANT `dfe_analyst_tier_1_role`" in s
        assert "dfe_org_" not in s  # unrestricted => no org role


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
            assert f"CREATE SETTINGS PROFILE IF NOT EXISTS `dfe_{t.name}_profile`" in s
            assert f"CREATE QUOTA IF NOT EXISTS `dfe_{t.name}_quota`" in s
