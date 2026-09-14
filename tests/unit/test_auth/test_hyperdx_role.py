#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_hyperdx_role.py
#  Purpose:      The role claim dfe-hyperdx reads, mapped off the engine's own roles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What each engine role becomes on the token the fork verifies."""

from __future__ import annotations

import pytest

from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.roles import RoleConfig


@pytest.mark.parametrize("role", sorted(hyperdx_role.TEAM_ADMIN_ROLES))
def test_a_team_admin_role_carries_a_value_the_fork_allows(role):
    claim = hyperdx_role.role_claim([role])
    assert claim == hyperdx_role.TEAM_ADMIN
    assert claim in hyperdx_role.FORK_ACCEPTS


@pytest.mark.parametrize(
    "role",
    sorted(set(RoleConfig.load_builtin().roles) - hyperdx_role.TEAM_ADMIN_ROLES),
)
def test_every_other_shipped_role_carries_a_value_the_fork_refuses(role):
    claim = hyperdx_role.role_claim([role])
    assert claim == hyperdx_role.MEMBER
    assert claim not in hyperdx_role.FORK_ACCEPTS


def test_an_account_with_no_role_at_all_still_carries_a_claim():
    # A missing claim is the fork's fallback to team membership, which is the
    # hole this closes, so the value is written even when there is no role.
    assert hyperdx_role.role_claim([]) == hyperdx_role.MEMBER


def test_one_team_admin_role_among_several_is_enough():
    assert hyperdx_role.role_claim(["data_viewer", "infra_admin"]) == hyperdx_role.TEAM_ADMIN
