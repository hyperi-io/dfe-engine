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

from collections.abc import Callable, Iterable
from typing import Any

from dfe_engine.auth.models import (
    AuthContext,
    AuthzResult,
    Scope,
    ScopedGrant,
    held_grants,
    is_platform_role,
)
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.orgs.tenant_scope import resolve_orgs

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
        "config:write",
        "deployment:read",
        "deployment:write",
        "fieldmap:read",
        "fieldmap:write",
        "hunt:execute",
        "hunt:read",
        "hunt:write",
        "org:ingest_credentials:write",
        "org:read",
        "org:write",
        "query:execute",
        "query:read",
        "raw_query:execute",
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
# each role can perform. The prefix is published with the scope catalogue
# (auth/rbac_scopes). The engine writes no Argo CD RBAC policy of its own.
ARGO_ACTION_PREFIX = "argo:"


def grant_covers(
    grant: ScopedGrant,
    requested: Scope,
    role_config: RoleConfig,
    orgs: Callable[[], Iterable[Any]] | None = None,
) -> bool:
    """Whether ``grant`` reaches ``requested``, holding a tenant-fenced system grant to its group's orgs.

    A system grant covers every scope (:meth:`Scope.covers`), except that a system
    grant of a role that is not a platform role (``scoped`` roles and ``org_viewer``,
    see :func:`~dfe_engine.auth.models.is_platform_role`) reaches an org, or a group an
    org owns, only when its own group names that org (``ScopedGrant.org_ids``). It
    reaches no other user's scope and no system-wide group's. Its system scope
    check still passes: a handler holds what it reads there to the same orgs.

    Args:
        grant: One of the caller's grants.
        requested: The scope the action is requested at.
        role_config: The role definitions ``authorize()`` reads.
        orgs: Reads the org registry's entries, called only when a fenced grant needs
            them, so a marker naming an org by tenant id resolves as
            :func:`~dfe_engine.orgs.tenant_scope.resolve_orgs` does. None compares the
            markers with the org's name.

    Returns:
        True when the grant covers the requested scope.
    """
    if not grant.scope.covers(requested):
        return False
    if grant.scope.type != "system" or requested.type == "system":
        return True
    if is_platform_role(grant.role, role_config):
        return True
    if requested.type == "user":
        return False
    org = requested.id if requested.type == "org" else requested.org
    if not org:
        return False
    if orgs is None:
        return org in grant.org_ids
    return org in {named.name for named in resolve_orgs(grant.org_ids, orgs())}


def authorize(
    auth: AuthContext | None,
    action: str,
    resource: str = "",
    *,
    scope: Scope | None = None,
    enabled: bool = True,
    role_config: RoleConfig | None = None,
    orgs: Callable[[], Iterable[Any]] | None = None,
) -> AuthzResult:
    """Evaluate whether an action is permitted at a scope.

    Grant-only union over the caller's scoped grants: a grant allows the
    action when its role's permission patterns match AND its scope covers
    the requested scope (:func:`grant_covers`). No deny rules, no shadowing -
    system grants are wider, except a tenant-fenced role's. ``scope=None`` is a SYSTEM-scope check ("no scope"
    never implicitly means "all scopes"), which keeps every existing
    unscoped require_action() call meaning what it always did: only
    system-wide role holders pass.

    Args:
        auth: Identity context (None = root mode in dev/test).
        action: Action string (e.g. "config:read").
        resource: Resource identifier (accepted, ignored -- for caller compat).
        scope: Requested scope. None means system scope.
        enabled: Whether auth is enabled. When False, all calls return allow.
        role_config: Override role configuration. Defaults to builtin roles.yaml.
        orgs: Reads the org registry's entries, as :func:`grant_covers` takes it.

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
    for grant in held_grants(auth):
        if grant_covers(grant, requested, config, orgs) and config.has_permission(
            grant.role, action
        ):
            # System grants keep the historical "role:<name>" reason form.
            suffix = "" if grant.scope.type == "system" else f"@{grant.scope}"
            return AuthzResult(allowed=True, reason=f"role:{grant.role}{suffix}")

    return AuthzResult(allowed=False, reason=f"no role grants '{action}' at scope '{requested}'")
