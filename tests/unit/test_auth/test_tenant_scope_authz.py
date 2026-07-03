#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Org-scoped tenant-action authorization - the scoped-rbac authz completion.

scoped-rbac made authorize() scope-aware; the app layer must decide which actions
an ORG-scoped grant may satisfy at the caller's OWN org. That set is DELIBERATELY
MINIMAL - query:execute only - because the parameterized-view executor is the one
handler that routes through the caller's per-org ClickHouse connection (org_id
injected + tenant_isolated guard). Every other action operates on a shared,
un-partitioned layer (global config writes, or raw-data reads via the admin
client), so an org grant must NOT satisfy it - else a tenant escalates to
global-config mutation or cross-org data disclosure.

These tests pin BOTH edges: the org-scoped viewer reaches query:execute, and
org-scoped grants are DENIED the config-write / raw-data actions that the first
(too-broad) cut wrongly allowed - the over-grant the adversarial re-verify caught.
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


def test_org_scoped_viewer_reaches_query_execute():
    # customer_viewer is the documented org-scoped role; query:execute is its core,
    # org-isolated capability (ViewExecutor injects org_id + tenant_isolated guard).
    u = _org_user("customer_viewer")
    assert _allowed(u, "query:execute")


def test_org_scoped_grant_denied_shared_config_and_raw_data():
    # The over-grant the adversarial re-verify caught. customer_viewer's grant also
    # lists source:read / sampler:read, but those are NOT tenant actions, so they
    # resolve system-only and are denied at org scope (shared config / admin-client
    # raw reads - not org-isolated).
    u = _org_user("customer_viewer")
    assert not _allowed(u, "source:read")
    assert not _allowed(u, "sampler:read")
    # An org-scoped ADMIN (org-bound '*') still cannot mutate global config or read
    # cross-org raw data: the escalation is closed at the action-set boundary, not
    # by trusting the role. It may only run the org-isolated view path.
    admin = _org_user("admin")
    assert _allowed(admin, "query:execute")
    for blocked in (
        "source:write",
        "source:delete",
        "schema:delete",
        "rule:write",
        "sampler:read",
        "discovery:read",
        "deployment:write",
    ):
        assert not _allowed(admin, blocked), blocked


def test_cross_org_denied():
    # An explicit request for another org's scope never passes for an acme grant.
    u = _org_user("customer_viewer", org="acme")
    assert not _allowed(u, "query:execute", Scope(type="org", id="other"))


def test_system_user_passes_tenant_and_system():
    u = _system_user()
    assert _allowed(u, "query:execute")
    assert _allowed(u, "deployment:write")


def test_tenant_actions_is_minimal_query_execute_only():
    # The safe set is EXACTLY {query:execute}. Guard against a future re-broadening
    # that re-opens the escalation: config writes and admin-client raw reads MUST
    # stay out until the resource layer is org-partitioned (the parked follow-up).
    # query:raw (the split-off arbitrary-SQL path) must never be a tenant action.
    assert TENANT_ACTIONS == frozenset({"query:execute"})
    for unsafe in (
        "source:write",
        "schema:delete",
        "sampler:read",
        "discovery:read",
        "source:read",
        "query:raw",
        "alert:write",
        "task:write",
    ):
        assert unsafe not in TENANT_ACTIONS
