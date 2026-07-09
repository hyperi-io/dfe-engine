#  Project:      dfe-engine
#  File:         governance/auth_sync.py
#  Purpose:      Mirror RBAC (roles+groups) into the gitops governance tree
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Publish the RBAC structure (roles + groups) into the gitops governance class.

Closes the survivability gap the audit flagged: roles/groups were raw local YAML,
lost on restart and absent from gitops. This mirrors them into the deploy repo
(governance/rbac/*) so authz is versioned + hand-editable + survives the engine,
and the rbac_source loader can read them back.

Accounts + API keys carry SECRET material (password/key hashes); they are NOT
mirrored here - they belong behind ESO/secret handling (follow-up), not in git.
"""

from __future__ import annotations

from dfe_engine.gitcrud import GitCrud

_ROLES_CLASS = "roles"
_GROUPS_CLASS = "groups"


def sync_rbac_to_gitops(crud: GitCrud, group_store, role_store, actor: str = "dfe-engine") -> int:
    """Write every role + group as YAML into the gitops governance class.

    Returns the number of resources written. One commit per resource (the engine's
    standard path); callers may batch by calling at a natural checkpoint.
    """
    written = 0

    for role in role_store.list():
        doc = {
            "description": role.description,
            "permissions": list(role.permissions),
            "scoped": role.scoped,
            "resource_type": role.resource_type,
        }
        crud.put(_ROLES_CLASS, role.name, doc, actor, message=f"rbac(role:{role.name}): sync")
        written += 1

    for group in group_store.list():
        doc = {
            "roles": list(group.roles),
            "members": list(group.members),
            "org_ids": list(group.org_ids),
        }
        crud.put(_GROUPS_CLASS, group.name, doc, actor, message=f"rbac(group:{group.name}): sync")
        written += 1

    return written
