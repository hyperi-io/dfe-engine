#  Project:      dfe-engine
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for auth models."""

from __future__ import annotations

import pytest

from dfe_engine.auth.models import AuthContext, AuthorizationError, AuthzResult


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


class TestAuthzResult:
    def test_allowed(self):
        r = AuthzResult(allowed=True, reason="role:admin")
        assert r.allowed
        assert r.reason == "role:admin"

    def test_denied(self):
        r = AuthzResult(allowed=False, reason="no role grants 'config:write'")
        assert not r.allowed


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
