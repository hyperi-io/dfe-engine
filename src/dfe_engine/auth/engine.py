"""DFE authorization engine.

Bespoke role→permission RBAC. Zero external dependencies.

The authorize(auth, action, resource) call signature maps 1:1 to the
Cedar/OPAL model. If DFE outgrows bespoke RBAC, swap the engine
implementation — callers don't change.
"""

from __future__ import annotations

from dfe_engine.auth.models import AuthContext, AuthzResult

# ---------------------------------------------------------------------------
# Action taxonomy
# ---------------------------------------------------------------------------
# Engine actions — DFE business logic (validated).
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

# Argo CD actions use the "argo:" prefix namespace.
# Format: argo:<resource>:<action>  (e.g. argo:applications:sync)
# These are NOT hardcoded — any argo:*:* action is valid.
# The RBAC YAML config is the source of truth for which argo actions
# each role can perform. The prefix is used to generate Argo CD
# argocd-rbac-cm policies from engine role definitions.
ARGO_ACTION_PREFIX = "argo:"

# ALL_ACTIONS contains only engine actions. Argo actions are open-ended
# (prefix-matched) so they cannot be enumerated here.
ALL_ACTIONS: set[str] = ENGINE_ACTIONS

# ---------------------------------------------------------------------------
# Built-in role→permission defaults.
# "*" is a wildcard granting all actions (engine + argo).
#
# Argo CD actions follow the format argo:<resource>:<action> where
# resource and action come directly from Argo CD's RBAC model:
#   resources: applications, clusters, repositories, projects, logs, exec
#   actions: get, create, update, delete, sync, override, action/*
# ---------------------------------------------------------------------------
DEFAULT_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "infra_admin": {
        # Engine: config + helm operations
        "config:read",
        "config:write",
        "helm:compile",
        "helm:execute_ddl",
        "helm:create_topics",
        # Argo CD: full application lifecycle + operational
        "argo:applications:get",
        "argo:applications:create",
        "argo:applications:update",
        "argo:applications:delete",
        "argo:applications:sync",
        "argo:applications:override",
        "argo:applications:action",
        "argo:clusters:get",
        "argo:repositories:get",
        "argo:repositories:create",
        "argo:repositories:update",
        "argo:projects:get",
        "argo:logs:get",
        "argo:exec:create",
    },
    "operator": {
        # Engine: data pipeline operations
        "config:read",
        "config:write",
        "source:read",
        "source:write",
        "query:execute",
        "helm:compile",
        # Argo CD: view + sync (no create/delete)
        "argo:applications:get",
        "argo:applications:sync",
        "argo:logs:get",
    },
    "viewer": {
        "config:read",
        "source:read",
        "query:execute",
        # Argo CD: read-only
        "argo:applications:get",
        "argo:projects:get",
    },
}


def authorize(
    auth: AuthContext | None,
    action: str,
    resource: str = "",
    *,
    enabled: bool = True,
    role_permissions: dict[str, set[str]] | None = None,
) -> AuthzResult:
    """Evaluate whether an action is permitted.

    Args:
        auth: Identity context (None = root mode in dev/test).
        action: Action string (e.g. "config:read").
        resource: Resource identifier (reserved for future use).
        enabled: Whether auth is enabled. When False, all calls return allow.
        role_permissions: Override role→permission map.

    Returns:
        AuthzResult with allowed=True/False and a reason string.
    """
    if not enabled:
        return AuthzResult(allowed=True, reason="auth_disabled")

    if auth is None:
        return AuthzResult(allowed=True, reason="root_mode")

    perms_map = role_permissions or DEFAULT_ROLE_PERMISSIONS

    for role in auth.roles:
        perms = perms_map.get(role, set())
        if "*" in perms or action in perms:
            return AuthzResult(allowed=True, reason=f"role:{role}")

    return AuthzResult(allowed=False, reason=f"no role grants '{action}'")
