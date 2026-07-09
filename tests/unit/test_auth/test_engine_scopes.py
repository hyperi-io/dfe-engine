#  Project:      dfe-engine
#  File:         test_engine_scopes.py
#  Purpose:      Scope-aware authorize() semantics
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""authorize() with scoped grants: grant-only union, no cross-org leaks."""

from __future__ import annotations

from dfe_engine.auth import AuthContext, Scope, ScopedGrant, authorize

SYSTEM = Scope()
ORG_ACME = Scope(type="org", id="acme")
ORG_GLOBEX = Scope(type="org", id="globex")
GROUP_IN_ACME = Scope(type="group", id="analysts", org="acme")


def _ctx(*grants: ScopedGrant) -> AuthContext:
    return AuthContext(
        user_id="u",
        roles=sorted({g.role for g in grants}),
        grants=list(grants),
    )


class TestOrgScopedGrants:
    def test_org_grant_denied_at_system_scope(self):
        ctx = _ctx(ScopedGrant(role="data_analyst", scope=ORG_ACME))
        assert not authorize(ctx, "hunt:read").allowed

    def test_org_grant_allowed_at_own_org(self):
        ctx = _ctx(ScopedGrant(role="data_analyst", scope=ORG_ACME))
        assert authorize(ctx, "hunt:read", scope=ORG_ACME).allowed

    def test_org_grant_denied_at_other_org(self):
        ctx = _ctx(ScopedGrant(role="data_analyst", scope=ORG_ACME))
        assert not authorize(ctx, "hunt:read", scope=ORG_GLOBEX).allowed

    def test_org_admin_wildcard_is_org_local(self):
        # "admin" role ("*") bound at org scope = full power inside the
        # org, nothing outside it (the K8s admin-in-namespace pattern).
        ctx = _ctx(ScopedGrant(role="admin", scope=ORG_ACME))
        assert authorize(ctx, "group:write", scope=ORG_ACME).allowed
        assert authorize(ctx, "group:write", scope=GROUP_IN_ACME).allowed
        assert not authorize(ctx, "group:write").allowed
        assert not authorize(ctx, "org:write").allowed
        assert not authorize(ctx, "group:write", scope=ORG_GLOBEX).allowed


class TestSystemGrants:
    def test_system_grant_covers_all_scopes(self):
        ctx = _ctx(ScopedGrant(role="data_analyst", scope=SYSTEM))
        assert authorize(ctx, "hunt:read").allowed
        assert authorize(ctx, "hunt:read", scope=ORG_ACME).allowed
        assert authorize(ctx, "hunt:read", scope=GROUP_IN_ACME).allowed

    def test_union_across_grants(self):
        ctx = _ctx(
            ScopedGrant(role="data_viewer", scope=SYSTEM),
            ScopedGrant(role="data_analyst", scope=ORG_ACME),
        )
        # analyst write only inside acme; viewer read everywhere
        assert authorize(ctx, "hunt:write", scope=ORG_ACME).allowed
        assert not authorize(ctx, "hunt:write").allowed
        assert authorize(ctx, "query:execute").allowed


class TestFallbackAndEdges:
    def test_bare_roles_mean_system_scope(self):
        # Contexts without grants (dev root, legacy construction) keep
        # historical behaviour: roles are system-wide.
        ctx = AuthContext(user_id="dev", roles=["admin"])
        assert authorize(ctx, "config:write").allowed
        assert authorize(ctx, "config:write", scope=ORG_ACME).allowed

    def test_no_grants_denies(self):
        ctx = AuthContext(user_id="nobody")
        result = authorize(ctx, "hunt:read")
        assert not result.allowed

    def test_denied_reason_names_scope(self):
        ctx = _ctx(ScopedGrant(role="data_analyst", scope=ORG_ACME))
        result = authorize(ctx, "hunt:read", scope=ORG_GLOBEX)
        assert "org:globex" in result.reason

    def test_auth_disabled_allows(self):
        assert authorize(None, "anything", enabled=False).allowed

    def test_root_mode_allows(self):
        assert authorize(None, "anything").allowed
