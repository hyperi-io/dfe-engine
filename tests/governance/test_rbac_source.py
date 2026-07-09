#  Project:      dfe-engine
#  File:         tests/governance/test_rbac_source.py
#  Purpose:      Tests for loading RBAC roles/groups from the gitops governance tree
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""RBAC-from-gitops loader (round-trips the auth_sync write format)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance.rbac_source import (
    load_groups,
    load_roles,
    resolve_roles_for_groups,
)


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    reg = ResourceClassRegistry(
        [
            ResourceClass("roles", "governance/rbac/roles", rbac_prefix="governance"),
            ResourceClass("groups", "governance/rbac/groups", rbac_prefix="governance"),
        ]
    )
    return GitCrud(repo, reg)


def test_load_roles_and_groups(crud):
    crud.put(
        "roles",
        "soc-ro",
        {"description": "ro", "permissions": ["governance:read", "helmvars:read"], "scoped": False},
        actor="x",
    )
    crud.put(
        "groups",
        "soc",
        {"roles": ["soc-ro"], "members": ["alice"], "org_ids": ["acme"]},
        actor="x",
    )
    roles = load_roles(crud)
    assert roles["soc-ro"]["permissions"] == ["governance:read", "helmvars:read"]
    groups = load_groups(crud)
    assert groups["soc"]["roles"] == ["soc-ro"]
    assert resolve_roles_for_groups(crud, ["soc"]) == ["soc-ro"]


def test_empty_is_empty(crud):
    assert load_roles(crud) == {}
    assert load_groups(crud) == {}
    assert resolve_roles_for_groups(crud, ["nope"]) == []
