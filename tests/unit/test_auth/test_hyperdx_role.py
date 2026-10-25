#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_hyperdx_role.py
#  Purpose:      The role claim dfe-hyperdx reads, mapped off the engine's own roles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What each engine role becomes on the token the fork verifies.

``admin`` changes what a whole team sees, ``member`` saves searches and dashboards,
and ``viewer`` is refused every write.
"""

import pytest

from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.models import Scope, ScopedGrant
from dfe_engine.auth.roles import RoleConfig, RoleDefinition

ACME = Scope(type="org", id="acme")
# The values on the wire, as dfe-hyperdx reads them.
ADMIN, MEMBER, VIEWER = "admin", "member", "viewer"

# Every shipped role, bound system-wide and bound at one org's scope.
SHIPPED = {
    "admin": (ADMIN, MEMBER),
    "data_analyst": (MEMBER, MEMBER),
    "data_analyst_viewer": (VIEWER, VIEWER),
    "data_viewer": (VIEWER, VIEWER),
    "dfe_operator": (VIEWER, VIEWER),
    "infra_admin": (ADMIN, VIEWER),
    "infra_viewer": (VIEWER, VIEWER),
    "org_viewer": (VIEWER, VIEWER),
}


def _system(*roles: str) -> list[ScopedGrant]:
    return [ScopedGrant(role=role) for role in roles]


def test_the_table_covers_every_shipped_role():
    assert set(SHIPPED) == set(RoleConfig.load_builtin().roles)


@pytest.mark.parametrize(("role", "expected"), sorted(SHIPPED.items()))
def test_each_shipped_role_carries_its_claim(role, expected):
    system, org_bound = expected

    assert hyperdx_role.role_claim(_system(role)) == system
    assert hyperdx_role.role_claim([ScopedGrant(role=role, scope=ACME)]) == org_bound


def test_only_a_team_admin_carries_a_value_the_fork_allows_a_team_wide_change():
    for role, (system, _org_bound) in SHIPPED.items():
        allowed = hyperdx_role.role_claim(_system(role)) in hyperdx_role.FORK_ACCEPTS
        assert allowed is (system == ADMIN), role


def test_an_account_with_no_role_at_all_is_a_viewer():
    # A missing claim is the fork's fallback to team membership, so the value is
    # written even when there is no role.
    assert hyperdx_role.role_claim([]) == VIEWER


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        (["query:write"], MEMBER),
        (["query:*"], MEMBER),
        (["*"], MEMBER),
        (["query:read", "query:execute", "dashboard:read"], VIEWER),
    ],
)
def test_a_custom_role_authors_only_with_query_write(permissions, expected):
    config = RoleConfig.load_builtin()
    config.roles["custom"] = RoleDefinition(
        description="a custom role", permissions=permissions, scoped=False
    )

    assert hyperdx_role.role_claim(_system("custom"), config) == expected


def test_a_custom_role_the_shipped_definitions_do_not_hold_reads_the_config_passed():
    assert hyperdx_role.role_claim(_system("custom")) == VIEWER


def test_one_team_admin_role_among_several_is_enough():
    assert hyperdx_role.role_claim(_system("data_viewer", "infra_admin")) == ADMIN


def test_one_authoring_role_among_viewers_makes_a_member():
    assert hyperdx_role.role_claim(_system("org_viewer", "data_analyst")) == MEMBER


def test_a_system_admin_beside_an_org_scoped_one_is_still_an_admin():
    grants = [ScopedGrant(role="admin", scope=ACME), ScopedGrant(role="infra_admin")]
    assert hyperdx_role.role_claim(grants) == ADMIN


@pytest.mark.parametrize(
    ("role", "expected"), [("admin", ADMIN), ("data_analyst", MEMBER), ("data_viewer", VIEWER)]
)
def test_a_context_with_bare_roles_reads_them_as_authorize_does(role, expected):
    """Auth disabled, the dev root and the local provider carry roles with no grants."""
    from dfe_engine.auth.models import AuthContext, held_grants

    context = AuthContext(user_id="dev", roles=[role])

    assert hyperdx_role.role_claim(held_grants(context)) == expected
