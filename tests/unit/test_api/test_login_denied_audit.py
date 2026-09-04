#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_login_denied_audit.py
#  Purpose:      a refused credential writes auth.login.denied at the endpoint that refused it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Where the login refusal audit event is written.

A wrong password and a failed OIDC callback each refuse a credential, so each
writes one `auth.login.denied`. Neither is written by the app-wide 401 handler,
which also answers expired tokens on ordinary requests - those are audited in
`deps.py` already, and counting them as logins would hide a brute force in the
request rate (issue #261).

Patching the audit logger IS the assertion here: these tests are about which
log events are emitted and from where, the same exception to the no-mocks
policy that tests/unit/test_auth/test_audit.py already takes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dfe_engine.auth.oidc.rp import NormalizedIdentity

AUDIT_LOGGER = "dfe_engine.auth.audit.logger"


def _denied(mock_logger) -> list:
    """Return the auth.login.denied calls seen by the patched audit logger."""
    return [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and call.args[0] == "auth.login.denied"
    ]


def _successes(mock_logger) -> list:
    """Return the auth.login.success calls seen by the patched audit logger."""
    return [
        call
        for call in mock_logger.info.call_args_list
        if call.args and call.args[0] == "auth.login.success"
    ]


class _FailingRp:
    """RP stub whose callback raises, as a bad code or a state mismatch does."""

    def has_provider(self, name: str) -> bool:
        return name == "stub"

    async def handle_callback(self, provider: str, request):
        raise ValueError("state mismatch")


class _SubjectlessRp:
    """RP stub whose callback returns an identity the IdP asserted no sub for."""

    def has_provider(self, name: str) -> bool:
        return name == "stub"

    async def handle_callback(self, provider: str, request) -> NormalizedIdentity:
        return NormalizedIdentity(subject="", email="alice@example.com")


class TestPasswordLoginRefusal:
    """POST /auth/login writes the event where the password is refused."""

    def test_bad_password_writes_one_denied_event(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
            )

        assert resp.status_code == 401
        events = _denied(mock_logger)
        assert len(events) == 1
        assert events[0].kwargs["user_id"] == "admin"
        assert events[0].kwargs["auth_path"] == "jwt"
        assert events[0].kwargs["reason"]

    def test_bad_password_writes_no_success_event(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
            )

        assert resp.status_code == 401
        assert _successes(mock_logger) == []

    def test_unknown_user_writes_one_denied_event(self, client: TestClient):
        """The attempted name is what makes a brute force readable."""
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "nosuchuser", "password": "wrong"},
            )

        assert resp.status_code == 401
        events = _denied(mock_logger)
        assert len(events) == 1
        assert events[0].kwargs["user_id"] == "nosuchuser"

    def test_accepted_password_writes_no_denied_event(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "test-admin-pw"},
            )

        assert resp.status_code == 200
        assert _denied(mock_logger) == []


class TestOidcCallbackRefusal:
    """The callback writes the event on both of its refusals."""

    @pytest.mark.parametrize("rp", [_FailingRp(), _SubjectlessRp()])
    def test_failed_callback_writes_one_denied_event(self, client: TestClient, app, rp):
        app.state.oidc_rp = rp

        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get("/api/v1/auth/oidc/stub/callback", follow_redirects=False)

        assert resp.status_code == 401
        events = _denied(mock_logger)
        assert len(events) == 1
        assert events[0].kwargs["auth_path"] == "oidc"
        assert events[0].kwargs["reason"]
