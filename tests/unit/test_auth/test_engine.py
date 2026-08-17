#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the bespoke authorization engine."""

from __future__ import annotations

import pytest

from dfe_engine.auth import (
    ARGO_ACTION_PREFIX,
    ENGINE_ACTIONS,
    AuthContext,
    AuthzResult,
    RoleConfig,
    RoleDefinition,
    authorize,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(roles: list[str], org_id: str = "acme", user_id: str = "alice") -> AuthContext:
    return AuthContext(org_id=org_id, user_id=user_id, roles=roles)


# ---------------------------------------------------------------------------
# Auth disabled
# ---------------------------------------------------------------------------


class TestAuthDisabled:
    def test_disabled_always_allows(self):
        result = authorize(None, "config:write", enabled=False)
        assert result.allowed
        assert result.reason == "auth_disabled"

    def test_disabled_allows_with_context(self):
        result = authorize(_auth(["data_viewer"]), "helm:execute_ddl", enabled=False)
        assert result.allowed
        assert result.reason == "auth_disabled"


# ---------------------------------------------------------------------------
# Root mode (auth=None, enabled=True)
# ---------------------------------------------------------------------------


class TestRootMode:
    def test_none_auth_returns_root_mode(self):
        result = authorize(None, "config:write", enabled=True)
        assert result.allowed
        assert result.reason == "root_mode"


# ---------------------------------------------------------------------------
# Admin role
# ---------------------------------------------------------------------------


class TestAdminRole:
    def test_admin_can_do_all_engine_actions(self):
        config = RoleConfig.load_builtin()
        for action in ENGINE_ACTIONS:
            result = authorize(_auth(["admin"]), action, enabled=True, role_config=config)
            assert result.allowed, f"admin should be allowed: {action}"
            assert result.reason == "role:admin"

    def test_admin_with_unknown_action(self):
        result = authorize(_auth(["admin"]), "foo:bar", enabled=True)
        assert result.allowed  # wildcard matches all


# ---------------------------------------------------------------------------
# data_analyst role (replaces old operator role)
# ---------------------------------------------------------------------------


class TestDataAnalystRole:
    @pytest.mark.parametrize(
        "action",
        [
            "hunt:read",
            "hunt:write",
            "query:execute",
            "query:read",
            "source:read",
            "source:write",
            "fieldmap:read",
            "fieldmap:write",
            "alert:read",
            "alert:write",
            "schema:read",
            "schema:write",
            "transforms:execute",
        ],
    )
    def test_data_analyst_allowed_actions(self, action: str):
        result = authorize(_auth(["data_analyst"]), action, enabled=True)
        assert result.allowed
        assert result.reason == "role:data_analyst"

    @pytest.mark.parametrize(
        "action",
        [
            "config:write",
            "helm:execute_ddl",
            "helm:create_topics",
            "argo:applications:delete",
            "deployment:write",
            "schema:delete",
        ],
    )
    def test_data_analyst_denied_actions(self, action: str):
        result = authorize(_auth(["data_analyst"]), action, enabled=True)
        assert not result.allowed
        assert "no role grants" in result.reason


# ---------------------------------------------------------------------------
# data_viewer role (replaces old viewer role)
# ---------------------------------------------------------------------------


class TestDataViewerRole:
    @pytest.mark.parametrize(
        "action",
        [
            "query:execute",
            "source:read",
            "dashboard:read",
        ],
    )
    def test_data_viewer_allowed_actions(self, action: str):
        result = authorize(_auth(["data_viewer"]), action, enabled=True)
        assert result.allowed
        assert result.reason == "role:data_viewer"

    @pytest.mark.parametrize(
        "action",
        [
            "config:write",
            "source:write",
            "helm:compile",
            "helm:execute_ddl",
            "helm:create_topics",
            "argo:applications:sync",
            "argo:applications:delete",
            "hunt:write",
            "fieldmap:write",
        ],
    )
    def test_data_viewer_denied_actions(self, action: str):
        result = authorize(_auth(["data_viewer"]), action, enabled=True)
        assert not result.allowed


# ---------------------------------------------------------------------------
# Multiple roles
# ---------------------------------------------------------------------------


class TestMultipleRoles:
    def test_first_matching_role_wins(self):
        result = authorize(
            _auth(["data_viewer", "data_analyst"]),
            "source:write",
            enabled=True,
        )
        assert result.allowed
        assert result.reason == "role:data_analyst"

    def test_admin_plus_viewer(self):
        result = authorize(
            _auth(["admin", "data_viewer"]),
            "helm:execute_ddl",
            enabled=True,
        )
        assert result.allowed
        assert result.reason == "role:admin"


# ---------------------------------------------------------------------------
# Unknown / no roles
# ---------------------------------------------------------------------------


class TestNoRoles:
    def test_no_roles_denied(self):
        result = authorize(_auth([]), "config:read", enabled=True)
        assert not result.allowed

    def test_unknown_role_denied(self):
        result = authorize(_auth(["intern"]), "config:read", enabled=True)
        assert not result.allowed
        assert "no role grants" in result.reason


# ---------------------------------------------------------------------------
# Custom role configuration
# ---------------------------------------------------------------------------


class TestCustomRolePermissions:
    def test_custom_role(self):
        custom = RoleConfig(
            roles={
                "soc_analyst": RoleDefinition(
                    description="SOC analyst",
                    permissions=["query:execute", "source:read"],
                )
            }
        )
        result = authorize(
            _auth(["soc_analyst"]),
            "query:execute",
            enabled=True,
            role_config=custom,
        )
        assert result.allowed
        assert result.reason == "role:soc_analyst"

    def test_custom_role_denied(self):
        custom = RoleConfig(
            roles={
                "soc_analyst": RoleDefinition(
                    description="SOC analyst",
                    permissions=["query:execute"],
                )
            }
        )
        result = authorize(
            _auth(["soc_analyst"]),
            "config:write",
            enabled=True,
            role_config=custom,
        )
        assert not result.allowed


# ---------------------------------------------------------------------------
# Resource parameter (forward-compat)
# ---------------------------------------------------------------------------


class TestResource:
    def test_resource_accepted_but_ignored_in_yaml_mode(self):
        result = authorize(
            _auth(["admin"]),
            "config:read",
            resource="ServiceConfig::receiver-production",
            enabled=True,
        )
        assert result.allowed


# ---------------------------------------------------------------------------
# AuthzResult model
# ---------------------------------------------------------------------------


class TestAuthzResult:
    def test_result_serialization(self):
        r = AuthzResult(allowed=True, reason="role:admin")
        data = r.model_dump()
        assert data == {"allowed": True, "reason": "role:admin"}


# ---------------------------------------------------------------------------
# infra_admin role
# ---------------------------------------------------------------------------


class TestInfraAdminRole:
    @pytest.mark.parametrize(
        "action",
        [
            "config:read",
            "config:write",
            "helm:compile",
            "helm:execute_ddl",
            "helm:create_topics",
            "argo:applications:get",
            "argo:applications:sync",
            "argo:applications:create",
            "argo:applications:delete",
            "argo:applications:action",
            "argo:exec:create",
            "deployment:read",
            "deployment:write",
            "service:loader:config:read",
            "service:receiver:metrics:read",
        ],
    )
    def test_infra_admin_allowed_actions(self, action: str):
        result = authorize(_auth(["infra_admin"]), action, enabled=True)
        assert result.allowed
        assert result.reason == "role:infra_admin"

    @pytest.mark.parametrize(
        "action",
        ["source:read", "source:write", "query:execute"],
    )
    def test_infra_admin_denied_data_actions(self, action: str):
        result = authorize(_auth(["infra_admin"]), action, enabled=True)
        assert not result.allowed

    def test_infra_admin_has_argo_wildcard(self):
        """infra_admin uses argo:* wildcard, not enumerated argo perms."""
        config = RoleConfig.load_builtin()
        role = config.roles["infra_admin"]
        argo_patterns = [p for p in role.permissions if p.startswith("argo:")]
        # Single wildcard pattern, not 10+ enumerated actions
        assert argo_patterns == ["argo:*"]

    def test_infra_admin_service_config_wildcard(self):
        """infra_admin has service:*:config:* — matches any service config."""
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra_admin", "service:loader:config:write")
        assert config.has_permission("infra_admin", "service:receiver:config:read")

    def test_infra_admin_service_metrics_read(self):
        """infra_admin has service:*:metrics:read — read any service metrics."""
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra_admin", "service:loader:metrics:read")
        assert not config.has_permission("infra_admin", "service:loader:metrics:write")


# ---------------------------------------------------------------------------
# Argo action namespace
# ---------------------------------------------------------------------------


class TestArgoActions:
    def test_argo_prefix(self):
        assert ARGO_ACTION_PREFIX == "argo:"

    def test_engine_actions_have_no_argo_prefix(self):
        for action in ENGINE_ACTIONS:
            assert not action.startswith("argo:")

    def test_argo_actions_are_open_ended(self):
        """Any argo:*:* action is valid -- infra_admin gets argo:* wildcard."""
        result = authorize(
            _auth(["infra_admin"]),
            "argo:applications:action",
            enabled=True,
        )
        assert result.allowed

    def test_infra_admin_allows_any_argo_action(self):
        """argo:* wildcard means any argo: action is permitted."""
        result = authorize(
            _auth(["infra_admin"]),
            "argo:applications:sync",
            enabled=True,
        )
        assert result.allowed

    def test_unknown_argo_action_denied_for_data_viewer(self):
        result = authorize(
            _auth(["data_viewer"]),
            "argo:applications:sync",
            enabled=True,
        )
        assert not result.allowed


# ---------------------------------------------------------------------------
# Default role configuration completeness
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_admin_has_wildcard(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("admin", "*")

    def test_data_analyst_has_no_ddl(self):
        config = RoleConfig.load_builtin()
        assert not config.has_permission("data_analyst", "helm:execute_ddl")
        assert not config.has_permission("data_analyst", "helm:create_topics")

    def test_data_viewer_is_read_only(self):
        config = RoleConfig.load_builtin()
        role = config.roles["data_viewer"]
        for perm in role.permissions:
            assert "write" not in perm
        assert not config.has_permission("data_viewer", "helm:compile")

    def test_infra_admin_exists(self):
        config = RoleConfig.load_builtin()
        assert "infra_admin" in config.roles

    def test_eight_built_in_roles(self):
        config = RoleConfig.load_builtin()
        assert len(config.roles) == 8

    def test_all_expected_roles_present(self):
        config = RoleConfig.load_builtin()
        expected = {
            "admin",
            "data_analyst",
            "data_analyst_viewer",
            "data_viewer",
            "infra_admin",
            "infra_viewer",
            "org_viewer",
            "dfe_operator",
        }
        assert set(config.roles.keys()) == expected

    def test_operator_holds_the_dials_and_nothing_else(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("dfe_operator", "action:invoke:receiver-surge")
        assert config.has_permission("dfe_operator", "governance:read")
        assert not config.has_permission("dfe_operator", "governance:write")
        assert not config.has_permission("dfe_operator", "helmvars:write")
        assert not config.has_permission("dfe_operator", "helmvars:read")

    def test_infra_admin_covers_governed_ops(self):
        config = RoleConfig.load_builtin()
        assert config.has_permission("infra_admin", "helmvars:write")
        assert config.has_permission("infra_admin", "governance:write")
        assert config.has_permission("infra_admin", "action:invoke:hunts-pause")
