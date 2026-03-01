"""Tests for auth models."""

import pytest

from dfe_engine.auth.models import AuthContext, AuthorizationError, AuthzRequest, AuthzResult


class TestAuthContext:
    def test_minimal(self):
        ctx = AuthContext(org_id="acme", user_id="alice")
        assert ctx.org_id == "acme"
        assert ctx.user_id == "alice"
        assert ctx.roles == []
        assert ctx.groups == []

    def test_full(self):
        ctx = AuthContext(
            org_id="acme",
            user_id="alice",
            roles=["admin"],
            permissions=["custom:perm"],
            groups=["dfe-admins"],
            request_id="req-123",
            client_ip="10.0.0.1",
            user_agent="curl/7.0",
        )
        assert ctx.roles == ["admin"]
        assert ctx.groups == ["dfe-admins"]
        assert ctx.request_id == "req-123"

    def test_serialization_roundtrip(self):
        ctx = AuthContext(org_id="acme", user_id="bob", roles=["viewer"])
        data = ctx.model_dump()
        restored = AuthContext.model_validate(data)
        assert restored == ctx


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
