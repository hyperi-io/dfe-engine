#  Project:      dfe-engine
#  File:         governance/rbac_source.py
#  Purpose:      Load RBAC (roles/groups) from the gitops governance class
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Read RBAC definitions from the gitops governance tree.

Survivability: roles + groups live in the deploy repo (governance/rbac/*), so authz
is versioned, hand-editable, and survives the engine. This loads them into the
shapes the existing RoleStore/GroupStore consume. The engine pulls latest on start.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.gitcrud import GitCrud

_ROLES_CLASS = "roles"
_GROUPS_CLASS = "groups"


def load_roles(crud: GitCrud) -> dict[str, dict[str, Any]]:
    """Return ``{role_name: {description, permissions, scoped}}`` from gitops."""
    roles: dict[str, dict[str, Any]] = {}
    for name in crud.list(_ROLES_CLASS):
        doc = crud.get(_ROLES_CLASS, name)
        roles[name] = {
            "description": doc.get("description", ""),
            "permissions": list(doc.get("permissions", [])),
            "scoped": bool(doc.get("scoped", False)),
            "resource_type": doc.get("resource_type", "custom"),
        }
    return roles


def load_groups(crud: GitCrud) -> dict[str, dict[str, Any]]:
    """Return ``{group_name: {roles, members, org_ids}}`` from gitops."""
    groups: dict[str, dict[str, Any]] = {}
    for name in crud.list(_GROUPS_CLASS):
        doc = crud.get(_GROUPS_CLASS, name)
        groups[name] = {
            "roles": list(doc.get("roles", [])),
            "members": list(doc.get("members", [])),
            "org_ids": list(doc.get("org_ids", [])),
        }
    return groups


def resolve_roles_for_groups(crud: GitCrud, group_names: list[str]) -> list[str]:
    """Accumulate the roles granted by a set of groups (gitops-sourced)."""
    groups = load_groups(crud)
    out: list[str] = []
    for g in group_names:
        for role in groups.get(g, {}).get("roles", []):
            if role not in out:
                out.append(role)
    return out
