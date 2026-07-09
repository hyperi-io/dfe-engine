#  Project:      dfe-engine
#  File:         tests/governance/test_auth_sync.py
#  Purpose:      Round-trip: sync real RoleStore/GroupStore to gitops, load back
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real (file-backed) RoleStore/GroupStore -> gitops -> rbac_source round-trip.

No docker needed - the auth stores are file-based. Proves the write format
(auth_sync) matches the read format (rbac_source).
"""

from __future__ import annotations

import pytest

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.role_store import RoleStore
from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance.auth_sync import sync_rbac_to_gitops
from dfe_engine.governance.rbac_source import load_groups, load_roles


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


def test_real_stores_round_trip_through_gitops(tmp_path, crud):
    (tmp_path / "roles.yaml").write_text("roles: {}\n")
    role_store = RoleStore(tmp_path / "roles.yaml")
    group_store = GroupStore(tmp_path / "groups")

    role_store.create("soc-ro", description="ro", permissions=["governance:read", "helmvars:read"])
    group_store.create("soc", roles=["soc-ro"], members=["alice"])

    written = sync_rbac_to_gitops(crud, group_store, role_store)
    assert written >= 2  # at least our role + group (plus any builtins)

    roles = load_roles(crud)
    assert "soc-ro" in roles
    assert roles["soc-ro"]["permissions"] == ["governance:read", "helmvars:read"]

    groups = load_groups(crud)
    assert groups["soc"]["roles"] == ["soc-ro"]
    assert groups["soc"]["members"] == ["alice"]
