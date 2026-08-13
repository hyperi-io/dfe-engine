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

import pytest

from dfe_engine.governance.ch.models import (
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    ChServiceRole,
    ChTier,
    GroupChBinding,
    org_user_name,
    tenant_policy_name,
)
from dfe_engine.governance.ch.render import (
    render_materialise,
    render_pinned_user,
    render_service_role,
    render_tenant_axis,
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

    def test_tenant_names(self):
        assert org_user_name("acme") == "dfe_org_acme"
        assert tenant_policy_name("dfe", "events") == "dfe_rowpol_tenant_dfe_events"

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
        # the role comes first; the profile then precedes the ALTER that carries it
        assert stmts[0] == "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`"
        assert stmts[1].startswith(
            "CREATE SETTINGS PROFILE IF NOT EXISTS `dfe_analyst_tier_2_profile`"
        )
        assert "readonly = 1" in stmts[1]
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

    def test_role_precedes_the_quota_assigned_to_it(self):
        """ClickHouse rejects a quota naming a role that does not exist yet.

        The failure is UNKNOWN_ROLE at reconcile time, and only on a cluster where
        the role was not already present - so a cluster that has run before hides
        it. Membership assertions cannot catch an ordering fault; this can.
        """
        stmts = render_tier(ChTier(name="t", quota={"interval": "1 hour", "queries": 1}))
        role_at = next(i for i, s in enumerate(stmts) if s.startswith("CREATE ROLE"))
        quota_at = next(i for i, s in enumerate(stmts) if s.startswith("CREATE QUOTA"))
        assert role_at < quota_at

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


class TestRenderTenantAxis:
    def test_shared_role_and_getsetting_policy(self):
        s = _joined(render_tenant_axis([("dfe", "events")]))
        assert "CREATE ROLE IF NOT EXISTS `dfe_tenant_role`" in s
        assert (
            "AS RESTRICTIVE FOR SELECT USING "
            "has(splitByChar(',', getSetting('SQL_current_tenant_id')), _org_id)" in s
        )
        assert "`dfe_rowpol_tenant_dfe_events` ON `dfe`.`events`" in s
        assert "PERMISSIVE" not in s  # HARD CONSTRAINT: restrictive-only

    def test_policy_per_table(self):
        stmts = render_tenant_axis([("dfe", "events"), ("dfe_hunts", "results")])
        s = _joined(stmts)
        assert s.count("CREATE ROW POLICY") == 2
        assert "`dfe_hunts`.`results`" in s

    def test_no_tables_just_role(self):
        assert render_tenant_axis([]) == ["CREATE ROLE IF NOT EXISTS `dfe_tenant_role`"]

    def test_no_per_org_objects(self):
        """The whole point of the shared axis: org count never changes it."""
        s = _joined(render_tenant_axis([("dfe", "events")]))
        assert "acme" not in s


class TestRenderPinnedUser:
    def test_org_tied_user_pins_and_holds_the_tenant_role(self):
        s = _joined(
            render_pinned_user(
                "dfe_org_acme", "abc123", tier_role="dfe_analyst_tier_2_role", org_ids=["acme"]
            )
        )
        assert (
            "CREATE USER IF NOT EXISTS `dfe_org_acme` IDENTIFIED WITH sha256_hash BY 'abc123'" in s
        )
        assert "GRANT `dfe_analyst_tier_2_role` TO `dfe_org_acme`" in s
        assert "GRANT `dfe_tenant_role` TO `dfe_org_acme`" in s
        # READONLY is the enforcement: an attacker SETTINGS override is a 452.
        assert "ALTER USER `dfe_org_acme` SETTINGS SQL_current_tenant_id = 'acme' READONLY" in s

    def test_multi_org_ids_join_into_one_pin(self):
        s = _joined(
            render_pinned_user("dfe_org_x", "h", tier_role="dfe_t_role", org_ids=["a", "b"])
        )
        assert "SETTINGS SQL_current_tenant_id = 'a,b' READONLY" in s

    def test_unrestricted_user_gets_no_pin_and_no_tenant_role(self):
        s = _joined(render_pinned_user("dfe_grp_admin", "h", tier_role="dfe_t_role", org_ids=[]))
        assert "GRANT `dfe_t_role`" in s
        assert "dfe_tenant_role" not in s
        assert "SQL_current_tenant_id" not in s

    def test_pin_escaping(self):
        s = _joined(
            render_pinned_user("dfe_org_x", "h", tier_role="dfe_t_role", org_ids=["o'brien"])
        )
        assert "SQL_current_tenant_id = 'o''brien'" in s

    def test_comma_in_org_id_is_rejected(self):
        """A comma would split into fragments that match nothing - refuse it."""
        with pytest.raises(ValueError):
            render_pinned_user("dfe_org_x", "h", tier_role="dfe_t_role", org_ids=["a,b"])


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
