#  Project:      dfe-engine
#  File:         rbac_scopes.py
#  Purpose:      Casbin-style permission scopes for role configuration UI
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Catalog of RBAC permission scopes (Casbin action strings) for role editors."""

from __future__ import annotations

from dfe_engine.auth.engine import API_ENFORCED_ACTIONS, ARGO_ACTION_PREFIX, ENGINE_ACTIONS
from dfe_engine.auth.roles import RoleConfig


def casbin_scopes_for_role_configuration() -> list[str]:
    """Return sorted permission patterns assignable in ``roles.yaml``.

    Includes built-in role patterns, engine-documented actions, and every
    action enforced by the HTTP API via ``require_action()``.
    """
    patterns: set[str] = set(ENGINE_ACTIONS)
    patterns.update(API_ENFORCED_ACTIONS)
    for role in RoleConfig.load_builtin().roles.values():
        patterns.update(role.permissions)
    return sorted(patterns)


def casbin_scope_catalog() -> dict[str, object]:
    """Metadata for UIs building role permission pickers."""
    return {
        "scopes": casbin_scopes_for_role_configuration(),
        "wildcard": True,
        "argo_namespace_prefix": ARGO_ACTION_PREFIX,
        "notes": (
            "Permission patterns use colon-separated segments. "
            "A trailing * matches any suffix (e.g. config:*). "
            "Mid-segment * matches one segment (e.g. service:*:config:read). "
            "Bare * grants all actions. "
            f"Argo CD actions use the {ARGO_ACTION_PREFIX!r} prefix."
        ),
    }
