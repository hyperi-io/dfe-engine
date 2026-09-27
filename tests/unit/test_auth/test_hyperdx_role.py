#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_hyperdx_role.py
#  Purpose:      The role claim dfe-hyperdx reads, mapped off the engine's own roles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What each engine role becomes on the token the fork verifies."""

import pytest

from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.models import Scope, ScopedGrant
from dfe_engine.auth.roles import RoleConfig

ACME = Scope(type="org", id="acme")


def _system(*roles: str) -> list[ScopedGrant]:
    return [ScopedGrant(role=role) for role in roles]


@pytest.mark.parametrize("role", sorted(hyperdx_role.TEAM_ADMIN_ROLES))
def test_a_team_admin_role_carries_a_value_the_fork_allows(role):
    claim = hyperdx_role.role_claim(_system(role))
    assert claim == hyperdx_role.TEAM_ADMIN
    assert claim in hyperdx_role.FORK_ACCEPTS


@pytest.mark.parametrize(
    "role",
    sorted(set(RoleConfig.load_builtin().roles) - hyperdx_role.TEAM_ADMIN_ROLES),
)
def test_every_other_shipped_role_carries_a_value_the_fork_refuses(role):
    claim = hyperdx_role.role_claim(_system(role))
    assert claim == hyperdx_role.MEMBER
    assert claim not in hyperdx_role.FORK_ACCEPTS


def test_an_account_with_no_role_at_all_still_carries_a_claim():
    # A missing claim is the fork's fallback to team membership, which is the
    # hole this closes, so the value is written even when there is no role.
    assert hyperdx_role.role_claim([]) == hyperdx_role.MEMBER


def test_one_team_admin_role_among_several_is_enough():
    assert hyperdx_role.role_claim(_system("data_viewer", "infra_admin")) == (
        hyperdx_role.TEAM_ADMIN
    )


@pytest.mark.parametrize("role", sorted(hyperdx_role.TEAM_ADMIN_ROLES))
def test_an_org_scoped_admin_is_a_member_of_the_fork(role):
    # The fork applies the claim to whichever team the token selects, so a grant
    # bound at one org's scope must not change what every team sees.
    claim = hyperdx_role.role_claim([ScopedGrant(role=role, scope=ACME)])
    assert claim == hyperdx_role.MEMBER


def test_a_system_admin_beside_an_org_scoped_one_is_still_an_admin():
    grants = [ScopedGrant(role="admin", scope=ACME), ScopedGrant(role="infra_admin")]
    assert hyperdx_role.role_claim(grants) == hyperdx_role.TEAM_ADMIN
