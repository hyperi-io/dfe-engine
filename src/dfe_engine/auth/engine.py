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

from dfe_engine.auth.models import AuthContext, AuthzResult, Scope, ScopedGrant
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
        "transform:compile",
        "transform:test",
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
    scope: Scope | None = None,
    enabled: bool = True,
    role_config: RoleConfig | None = None,
) -> AuthzResult:
    """Evaluate whether an action is permitted at a scope.

    Grant-only union over the caller's scoped grants: a grant allows the
    action when its role's permission patterns match AND its scope covers
    the requested scope. No deny rules, no shadowing - system grants are
    simply wider. ``scope=None`` is a SYSTEM-scope check ("no scope"
    never implicitly means "all scopes"), which keeps every existing
    unscoped require_action() call meaning what it always did: only
    system-wide role holders pass.

    Args:
        auth: Identity context (None = root mode in dev/test).
        action: Action string (e.g. "config:read").
        resource: Resource identifier (accepted, ignored -- for caller compat).
        scope: Requested scope; None means system scope.
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
    requested = scope if scope is not None else Scope()

    # Contexts built without scoped grants (dev root, tests, legacy JWT)
    # carry bare role names - those have always meant system-wide.
    grants = auth.grants or [ScopedGrant(role=name) for name in auth.roles]

    for grant in grants:
        if grant.scope.covers(requested) and config.has_permission(grant.role, action):
            # System grants keep the historical "role:<name>" reason form.
            suffix = "" if grant.scope.type == "system" else f"@{grant.scope}"
            return AuthzResult(allowed=True, reason=f"role:{grant.role}{suffix}")

    return AuthzResult(allowed=False, reason=f"no role grants '{action}' at scope '{requested}'")
