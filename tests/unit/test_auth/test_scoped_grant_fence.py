#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_scoped_grant_fence.py
#  Purpose:      authorize() holds a system-bound tenant role to the orgs its group names
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A system grant of a ``scoped`` role, or ``org_viewer``, reaches only its group's orgs.

Every row is one ``authorize()`` call: the grant, the scope asked at, the action, and the
answer. The platform role and the org-bound grant are the controls that must not move.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.auth.engine import authorize
from dfe_engine.auth.models import AuthContext, Scope, ScopedGrant
from dfe_engine.auth.roles import RoleConfig, RoleDefinition

ACME = Scope(type="org", id="acme")
BETA = Scope(type="org", id="beta")
ACME_GROUP = Scope(type="group", id="acme-team", org="acme")
BETA_GROUP = Scope(type="group", id="beta-team", org="beta")
WIDE_GROUP = Scope(type="group", id="wide", org="")
SOMEONE = Scope(type="user", id="someone")
SYSTEM = Scope()

ORGS = [
    SimpleNamespace(name="acme", org_ids=["t-acme"]),
    SimpleNamespace(name="beta", org_ids=[]),
]


@pytest.fixture
def roles() -> RoleConfig:
    config = RoleConfig.load_builtin()
    config.roles["tenant"] = RoleDefinition(
        description="a tenant role",
        permissions=["org:read", "org:write", "group:write", "query:execute"],
        scoped=True,
    )
    return config


def _caller(role: str, scope: Scope = SYSTEM, *markers: str) -> AuthContext:
    grant = ScopedGrant(role=role, scope=scope, org_ids=markers)
    return AuthContext(user_id="u", roles=[role], grants=[grant])


def _allowed(caller, action, scope, roles, orgs=None) -> bool:
    registry = None if orgs is None else lambda: orgs
    return authorize(caller, action, scope=scope, role_config=roles, orgs=registry).allowed


@pytest.mark.parametrize(
    ("scope", "action", "allowed"),
    [
        pytest.param(ACME, "org:read", True, id="the-named-org"),
        pytest.param(BETA, "org:read", False, id="another-org-read"),
        pytest.param(BETA, "org:write", False, id="another-org-write"),
        pytest.param(ACME_GROUP, "group:write", True, id="the-named-orgs-group"),
        pytest.param(BETA_GROUP, "group:write", False, id="another-orgs-group"),
        pytest.param(WIDE_GROUP, "group:write", False, id="a-system-wide-group"),
        pytest.param(SOMEONE, "org:read", False, id="another-users-scope"),
        pytest.param(SYSTEM, "org:read", True, id="a-system-read-its-handler-holds"),
        pytest.param(SYSTEM, "query:execute", True, id="a-system-query"),
    ],
)
def test_a_system_bound_scoped_role_is_held_to_its_groups_orgs(roles, scope, action, allowed):
    caller = _caller("tenant", SYSTEM, "acme")

    assert _allowed(caller, action, scope, roles) is allowed


@pytest.mark.parametrize(
    ("marker", "scope", "allowed"),
    [
        pytest.param("t-acme", ACME, True, id="a-tenant-id-names-its-org"),
        pytest.param("t-acme", BETA, False, id="a-tenant-id-names-no-other-org"),
        pytest.param("acme", ACME, True, id="a-name-names-its-org"),
        pytest.param("nowhere", ACME, False, id="a-marker-no-org-declares"),
    ],
)
def test_markers_resolve_against_the_registry_as_held_reads_do(roles, marker, scope, allowed):
    caller = _caller("tenant", SYSTEM, marker)

    assert _allowed(caller, "org:read", scope, roles, ORGS) is allowed


def test_without_a_registry_a_marker_matches_the_org_name_only(roles):
    by_id = _caller("tenant", SYSTEM, "t-acme")

    assert _allowed(by_id, "org:read", ACME, roles) is False
    assert _allowed(_caller("tenant", SYSTEM, "acme"), "org:read", ACME, roles) is True


def test_a_group_naming_no_org_reaches_none(roles):
    caller = _caller("tenant")

    assert _allowed(caller, "org:read", ACME, roles) is False


def test_org_viewer_bound_system_wide_is_held_the_same_way(roles):
    caller = _caller("org_viewer", SYSTEM, "acme")

    assert _allowed(caller, "source:read", ACME, roles) is True
    assert _allowed(caller, "source:read", BETA, roles) is False
    assert _allowed(caller, "source:read", SYSTEM, roles) is True


def test_a_bare_role_carries_the_contexts_orgs():
    roles = RoleConfig.load_builtin()
    roles.roles["tenant"] = RoleDefinition(
        description="a tenant role", permissions=["org:read"], scoped=True
    )
    caller = AuthContext(user_id="u", roles=["tenant"], org_ids=["acme"])

    assert _allowed(caller, "org:read", ACME, roles) is True
    assert _allowed(caller, "org:read", BETA, roles) is False


@pytest.mark.parametrize("scope", [ACME, BETA, ACME_GROUP, WIDE_GROUP, SOMEONE, SYSTEM])
def test_a_platform_role_bound_system_wide_still_reaches_every_scope(roles, scope):
    caller = _caller("admin")

    assert _allowed(caller, "org:write", scope, roles, ORGS) is True


@pytest.mark.parametrize(
    ("scope", "allowed"),
    [pytest.param(ACME, True, id="its-org"), pytest.param(BETA, False, id="another-org")],
)
def test_a_tenant_role_bound_at_an_orgs_scope_is_unchanged(roles, scope, allowed):
    caller = _caller("tenant", ACME, "acme")

    assert _allowed(caller, "org:write", scope, roles) is allowed
