"""Tests for the bespoke authorization engine."""

import pytest

from dfe_engine.auth import (
    ALL_ACTIONS,
    ARGO_ACTION_PREFIX,
    DEFAULT_ROLE_PERMISSIONS,
    ENGINE_ACTIONS,
    AuthContext,
    AuthzResult,
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
        result = authorize(_auth(["viewer"]), "helm:execute_ddl", enabled=False)
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
    def test_admin_can_do_everything(self):
        for action in ALL_ACTIONS:
            result = authorize(_auth(["admin"]), action, enabled=True)
            assert result.allowed, f"admin should be allowed: {action}"
            assert result.reason == "role:admin"

    def test_admin_with_unknown_action(self):
        result = authorize(_auth(["admin"]), "foo:bar", enabled=True)
        assert result.allowed  # wildcard matches all


# ---------------------------------------------------------------------------
# Operator role
# ---------------------------------------------------------------------------


class TestOperatorRole:
    @pytest.mark.parametrize(
        "action",
        [
            "config:read",
            "config:write",
            "source:read",
            "source:write",
            "query:execute",
            "helm:compile",
            "argo:applications:get",
            "argo:applications:sync",
            "argo:logs:get",
        ],
    )
    def test_operator_allowed_actions(self, action):
        result = authorize(_auth(["operator"]), action, enabled=True)
        assert result.allowed
        assert result.reason == "role:operator"

    @pytest.mark.parametrize(
        "action",
        [
            "helm:execute_ddl",
            "helm:create_topics",
            "argo:applications:delete",
            "argo:clusters:create",
        ],
    )
    def test_operator_denied_actions(self, action):
        result = authorize(_auth(["operator"]), action, enabled=True)
        assert not result.allowed
        assert "no role grants" in result.reason


# ---------------------------------------------------------------------------
# Viewer role
# ---------------------------------------------------------------------------


class TestViewerRole:
    @pytest.mark.parametrize(
        "action",
        [
            "config:read",
            "source:read",
            "query:execute",
            "argo:applications:get",
            "argo:projects:get",
        ],
    )
    def test_viewer_allowed_actions(self, action):
        result = authorize(_auth(["viewer"]), action, enabled=True)
        assert result.allowed
        assert result.reason == "role:viewer"

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
        ],
    )
    def test_viewer_denied_actions(self, action):
        result = authorize(_auth(["viewer"]), action, enabled=True)
        assert not result.allowed


# ---------------------------------------------------------------------------
# Multiple roles
# ---------------------------------------------------------------------------


class TestMultipleRoles:
    def test_first_matching_role_wins(self):
        result = authorize(_auth(["viewer", "operator"]), "config:write", enabled=True)
        assert result.allowed
        assert result.reason == "role:operator"

    def test_admin_plus_viewer(self):
        result = authorize(_auth(["admin", "viewer"]), "helm:execute_ddl", enabled=True)
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
# Custom role permissions
# ---------------------------------------------------------------------------


class TestCustomRolePermissions:
    def test_custom_role(self):
        custom = {"soc_analyst": {"query:execute", "source:read"}}
        result = authorize(
            _auth(["soc_analyst"]),
            "query:execute",
            enabled=True,
            role_permissions=custom,
        )
        assert result.allowed
        assert result.reason == "role:soc_analyst"

    def test_custom_role_denied(self):
        custom = {"soc_analyst": {"query:execute"}}
        result = authorize(
            _auth(["soc_analyst"]),
            "config:write",
            enabled=True,
            role_permissions=custom,
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
# Default role permissions completeness
# ---------------------------------------------------------------------------


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
        ],
    )
    def test_infra_admin_allowed_actions(self, action):
        result = authorize(_auth(["infra_admin"]), action, enabled=True)
        assert result.allowed
        assert result.reason == "role:infra_admin"

    @pytest.mark.parametrize("action", ["source:read", "source:write", "query:execute"])
    def test_infra_admin_denied_data_actions(self, action):
        result = authorize(_auth(["infra_admin"]), action, enabled=True)
        assert not result.allowed

    def test_infra_admin_has_full_argo_lifecycle(self):
        perms = DEFAULT_ROLE_PERMISSIONS["infra_admin"]
        argo_perms = {p for p in perms if p.startswith(ARGO_ACTION_PREFIX)}
        assert len(argo_perms) >= 10  # comprehensive argo access


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
        """Any argo:*:* action is valid — not enumerated in ALL_ACTIONS."""
        result = authorize(
            _auth(["infra_admin"]),
            "argo:applications:action",
            enabled=True,
        )
        assert result.allowed

    def test_unknown_argo_action_denied_for_viewer(self):
        result = authorize(
            _auth(["viewer"]),
            "argo:applications:sync",
            enabled=True,
        )
        assert not result.allowed


# ---------------------------------------------------------------------------
# Default role permissions completeness
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_all_actions_covered_by_admin(self):
        assert "*" in DEFAULT_ROLE_PERMISSIONS["admin"]

    def test_operator_has_no_ddl(self):
        perms = DEFAULT_ROLE_PERMISSIONS["operator"]
        assert "helm:execute_ddl" not in perms
        assert "helm:create_topics" not in perms

    def test_viewer_is_read_only(self):
        perms = DEFAULT_ROLE_PERMISSIONS["viewer"]
        assert all("write" not in p for p in perms)
        assert "helm:compile" not in perms

    def test_infra_admin_exists(self):
        assert "infra_admin" in DEFAULT_ROLE_PERMISSIONS

    def test_four_built_in_roles(self):
        assert len(DEFAULT_ROLE_PERMISSIONS) == 4
