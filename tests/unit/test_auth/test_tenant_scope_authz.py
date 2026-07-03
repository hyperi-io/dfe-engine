#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Org-scoped tenant-action authorization - the scoped-rbac authz completion.

Exercises the gap the /review found: an org-scoped group member must reach their
OWN org's data plane (query/source/sampler/...), while system/admin actions stay
system-only and cross-org is denied. The rest of the suite only provisions
system-scoped groups, so this path was previously unexercised - a silent 403 for
every org-scoped deployment (the documented customer_viewer path).
"""

from __future__ import annotations

from dfe_engine.api.deps import TENANT_ACTIONS, _authorize_resolved
from dfe_engine.auth import AuthContext, Scope, ScopedGrant
from dfe_engine.auth.roles import RoleConfig

_RC = RoleConfig.load_builtin()


def _org_user(role: str, org: str = "acme") -> AuthContext:
    return AuthContext(
        user_id="u",
        org_ids=[org],
        grants=[ScopedGrant(role=role, scope=Scope(type="org", id=org))],
    )


def _system_user(role: str = "admin") -> AuthContext:
    return AuthContext(user_id="root", grants=[ScopedGrant(role=role, scope=Scope())])


def _allowed(user: AuthContext, action: str, scope: Scope | None = None) -> bool:
    return _authorize_resolved(user, action, scope, enabled=True, role_config=_RC).allowed


def test_org_scoped_viewer_reaches_own_org_tenant_actions():
    u = _org_user("customer_viewer")
    assert _allowed(u, "query:execute")
    assert _allowed(u, "source:read")
    assert _allowed(u, "sampler:read")


def test_org_scoped_admin_denied_system_action_but_allowed_tenant():
    # An org-scoped admin (admin grants '*') still gets tenant actions at its org,
    # but a NON-tenant/system action stays system-only and is denied - proving the
    # boundary (org grants never satisfy a system check).
    u = _org_user("admin")
    assert _allowed(u, "query:execute")
    assert not _allowed(u, "deployment:write")


def test_cross_org_denied():
    u = _org_user("customer_viewer", org="acme")
    assert not _allowed(u, "query:execute", Scope(type="org", id="other"))


def test_system_user_passes_tenant_and_system():
    u = _system_user()
    assert _allowed(u, "query:execute")
    assert _allowed(u, "deployment:write")


def test_transform_actions_are_tenant_and_singular():
    # data_analyst grants transform:* (singular, post spelling fix); an org-scoped
    # analyst can compile/test transforms for its org.
    u = _org_user("data_analyst")
    assert "transform:compile" in TENANT_ACTIONS
    assert _allowed(u, "transform:compile")
    assert _allowed(u, "transform:test")
