#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for auth models."""

import pytest

from dfe_engine.auth.models import (
    AuthContext,
    AuthorizationError,
    AuthzRequest,
    AuthzResult,
    Scope,
    ScopedGrant,
    platform_caller,
)

ORG_SCOPE = Scope(type="org", id="acme")


class TestAuthContext:
    def test_minimal(self):
        ctx = AuthContext(user_id="alice")
        assert ctx.org_id == "default"
        assert ctx.user_id == "alice"
        assert ctx.roles == []
        assert ctx.groups == []
        assert ctx.org_ids == []
        assert ctx.connection_id == ""

    def test_full(self):
        ctx = AuthContext(
            org_id="acme",
            user_id="alice",
            roles=["admin"],
            org_ids=["acme", "globex"],
            connection_id="ch-acme",
            groups=["dfe-admins"],
            request_id="req-123",
            client_ip="10.0.0.1",
            user_agent="curl/7.0",
        )
        assert ctx.roles == ["admin"]
        assert ctx.groups == ["dfe-admins"]
        assert ctx.request_id == "req-123"
        assert ctx.org_ids == ["acme", "globex"]
        assert ctx.connection_id == "ch-acme"

    def test_default_org_id(self):
        ctx = AuthContext(user_id="bob")
        assert ctx.org_id == "default"

    def test_explicit_org_id(self):
        ctx = AuthContext(org_id="acme", user_id="bob")
        assert ctx.org_id == "acme"

    def test_org_ids_field(self):
        ctx = AuthContext(user_id="bob", org_ids=["acme", "globex"])
        assert ctx.org_ids == ["acme", "globex"]

    def test_connection_id_field(self):
        ctx = AuthContext(user_id="bob", connection_id="ch-default")
        assert ctx.connection_id == "ch-default"

    def test_serialization_roundtrip(self):
        ctx = AuthContext(org_id="acme", user_id="bob", roles=["data_viewer"])
        data = ctx.model_dump()
        restored = AuthContext.model_validate(data)
        assert restored == ctx


class TestPlatformCaller:
    def test_keeps_only_the_system_grants_beyond_org_viewer(self):
        user = AuthContext(
            user_id="alice",
            roles=["data_viewer", "org_viewer", "admin"],
            grants=[
                ScopedGrant(role="data_viewer"),
                ScopedGrant(role="org_viewer"),
                ScopedGrant(role="admin", scope=ORG_SCOPE),
            ],
            org_ids=["acme"],
        )

        caller = platform_caller(user)

        assert caller is not None
        assert caller.grants == [ScopedGrant(role="data_viewer")]
        assert caller.roles == ["data_viewer"]
        assert caller.user_id == "alice"
        assert caller.org_ids == ["acme"]
        assert user.roles == ["data_viewer", "org_viewer", "admin"]

    def test_reads_bare_roles_as_system_grants(self):
        caller = platform_caller(AuthContext(user_id="bob", roles=["org_viewer", "data_analyst"]))

        assert caller is not None
        assert caller.grants == [ScopedGrant(role="data_analyst")]
        assert caller.roles == ["data_analyst"]

    @pytest.mark.parametrize(
        "user",
        [
            AuthContext(user_id="carol"),
            AuthContext(user_id="carol", roles=["org_viewer"]),
            AuthContext(
                user_id="carol",
                roles=["admin"],
                grants=[ScopedGrant(role="admin", scope=ORG_SCOPE)],
            ),
        ],
        ids=["no roles", "org_viewer only", "bound at one org"],
    )
    def test_is_none_for_a_caller_without_a_platform_grant(self, user):
        assert platform_caller(user) is None


class TestAuthzResult:
    def test_allowed(self):
        r = AuthzResult(allowed=True, reason="role:admin")
        assert r.allowed
        assert r.reason == "role:admin"

    def test_denied(self):
        r = AuthzResult(allowed=False, reason="no role grants 'config:write'")
        assert not r.allowed


class TestAuthzRequest:
    def test_create(self):
        req = AuthzRequest(
            principal='User::"alice"',
            action='Action::"config:read"',
            resource='ServiceConfig::"receiver-production"',
        )
        assert req.principal == 'User::"alice"'
        assert req.context == {}


class TestAuthorizationError:
    def test_is_exception(self):
        with pytest.raises(AuthorizationError):
            raise AuthorizationError("access denied")


class TestBackwardCompat:
    """Verify that query.models still re-exports auth types."""

    def test_authcontext_from_query_models(self):
        from dfe_engine.query.models import AuthContext as QAuthContext

        ctx = QAuthContext(org_id="acme", user_id="bob")
        assert ctx.org_id == "acme"

    def test_authorizationerror_from_query_models(self):
        from dfe_engine.query.models import AuthorizationError as QAuthError

        with pytest.raises(QAuthError):
            raise QAuthError("denied")
