#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""DFE authorization engine.

Bespoke role->permission RBAC backed by YAML role definitions.

The authorize(auth, action, resource) call signature maps 1:1 to the
Cedar/OPAL model. If DFE outgrows bespoke RBAC, swap the engine
implementation -- callers don't change.
"""

from __future__ import annotations

from dfe_engine.auth.models import AuthContext, AuthzResult
from dfe_engine.auth.roles import RoleConfig

# ---------------------------------------------------------------------------
# Action taxonomy
# ---------------------------------------------------------------------------
# Engine actions -- DFE business logic (documentation-only reference set).
ENGINE_ACTIONS: set[str] = {
    "config:read",
    "config:write",
    "source:read",
    "source:write",
    "query:execute",
    "helm:compile",
    "helm:execute_ddl",
    "helm:create_topics",
}

# Actions enforced on REST handlers via require_action() (documentation catalog).
API_ENFORCED_ACTIONS: frozenset[str] = frozenset(
    {
        "alert:read",
        "alert:write",
        "config:read",
        "config:write",
        "deployment:read",
        "deployment:write",
        "discovery:read",
        "fieldmap:read",
        "fieldmap:write",
        "hunt:execute",
        "hunt:read",
        "hunt:write",
        "org:read",
        "org:write",
        "query:execute",
        "query:read",
        "query:write",
        "schema:delete",
        "schema:read",
        "schema:write",
        "source:read",
        "source:write",
        "transforms:compile",
        "transforms:test",
    }
)

# Argo CD actions use the "argo:" prefix namespace.
# Format: argo:<resource>:<action>  (e.g. argo:applications:sync)
# These are NOT hardcoded -- any argo:*:* action is valid.
# The RBAC YAML config is the source of truth for which argo actions
# each role can perform. The prefix is used to generate Argo CD
# argocd-rbac-cm policies from engine role definitions.
ARGO_ACTION_PREFIX = "argo:"


def authorize(
    auth: AuthContext | None,
    action: str,
    resource: str = "",
    *,
    enabled: bool = True,
    role_config: RoleConfig | None = None,
) -> AuthzResult:
    """Evaluate whether an action is permitted.

    Args:
        auth: Identity context (None = root mode in dev/test).
        action: Action string (e.g. "config:read").
        resource: Resource identifier (accepted, ignored -- for caller compat).
        enabled: Whether auth is enabled. When False, all calls return allow.
        role_config: Override role configuration. Defaults to builtin roles.yaml.

    Returns:
        AuthzResult with allowed=True/False and a reason string.
    """
    if not enabled:
        return AuthzResult(allowed=True, reason="auth_disabled")

    if auth is None:
        return AuthzResult(allowed=True, reason="root_mode")

    config = role_config or RoleConfig.load_builtin()

    granting_role = config.check_roles(auth.roles, action)
    if granting_role is not None:
        return AuthzResult(allowed=True, reason=f"role:{granting_role}")

    return AuthzResult(allowed=False, reason=f"no role grants '{action}'")
