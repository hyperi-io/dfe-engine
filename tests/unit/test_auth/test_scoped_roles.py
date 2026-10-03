#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_scoped_roles.py
#  Purpose:      A role marked scoped never reads across orgs, wherever it is bound
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``platform_grants`` reads the role definitions' ``scoped`` flag.

The roles API documents ``scoped: true`` as org-scoped, so a custom tenant role
bound at system scope must not read every org. ``org_viewer`` stays a tenant
role whatever a deployment's definitions say about it.
"""

from typing import get_type_hints

from dfe_engine.auth.models import AuthContext, ScopedGrant, platform_caller, platform_grants
from dfe_engine.auth.roles import RoleConfig, RoleDefinition

TENANT = RoleDefinition(description="a tenant's viewer", permissions=["sampler:read"], scoped=True)


def _definitions(**roles: RoleDefinition) -> RoleConfig:
    return RoleConfig({**RoleConfig.load_builtin().roles, **roles})


def test_a_scoped_role_at_system_scope_never_reads_across_orgs():
    grants = [ScopedGrant(role="tenant_viewer")]

    assert platform_grants(grants, role_config=_definitions(tenant_viewer=TENANT)) == []


def test_the_same_role_unscoped_does():
    grant = ScopedGrant(role="tenant_viewer")
    unscoped = TENANT.model_copy(update={"scoped": False})

    assert platform_grants([grant], role_config=_definitions(tenant_viewer=unscoped)) == [grant]


def test_org_viewer_stays_a_tenant_role_when_a_deployment_drops_its_flag():
    unflagged = RoleDefinition(description="viewer", permissions=["sampler:read"])
    grants = [ScopedGrant(role="org_viewer")]

    assert platform_grants(grants, role_config=_definitions(org_viewer=unflagged)) == []


def test_a_role_the_definitions_do_not_declare_unfences_nothing():
    grants = [ScopedGrant(role="no_such_role")]

    assert platform_grants(grants, role_config=RoleConfig.load_builtin()) == []


def test_the_shipped_definitions_are_read_when_none_are_passed():
    grants = [ScopedGrant(role="org_viewer"), ScopedGrant(role="data_viewer")]

    assert platform_grants(grants) == [ScopedGrant(role="data_viewer")]


def test_a_caller_holding_only_a_scoped_role_is_no_platform_caller():
    user = AuthContext(user_id="tenant", roles=["tenant_viewer"], org_ids=["acme"])

    assert platform_caller(user, role_config=_definitions(tenant_viewer=TENANT)) is None


def test_platform_caller_type_hints_resolve():
    hints = get_type_hints(platform_caller)

    assert hints["user"] is AuthContext
    assert hints["return"] == AuthContext | None
