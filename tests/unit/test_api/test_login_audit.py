#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_login_audit.py
#  Purpose:      auth.login.success is written on a credential exchange, not per request
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Where the login audit event is written.

`auth.login.success` is the count of logins, so it belongs to the endpoints
that exchange a credential for a token - password login and the OIDC callback.
Validating a token on a request must write nothing, or the audit trail reports
the request rate instead (issue #257).

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


def _login_successes(mock_logger) -> list:
    """Return the auth.login.success calls seen by the patched audit logger."""
    return [
        call
        for call in mock_logger.info.call_args_list
        if call.args and call.args[0] == "auth.login.success"
    ]


class _CallbackRp:
    """RP stub whose callback returns a fixed identity (no live IdP)."""

    def has_provider(self, name: str) -> bool:
        return name == "stub"

    async def handle_callback(self, provider: str, request) -> NormalizedIdentity:
        return NormalizedIdentity(
            subject="alice@example.com",
            email="alice@example.com",
            groups=["dfe-admins"],
        )


@pytest.fixture
def api_key(app) -> str:
    """A usable API key for the machine-to-machine path."""
    group_store = app.state.group_store
    if group_store.get("audit-ops") is None:
        group_store.create("audit-ops", roles=["infra_admin"])
    _, full_key = app.state.api_key_store.create("audit-key", groups=["audit-ops"])
    return full_key


class TestCredentialExchangeWritesTheEvent:
    """The endpoints that mint a token write exactly one event."""

    def test_password_login_writes_one_event(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "test-admin-pw"},
            )

        assert resp.status_code == 200
        events = _login_successes(mock_logger)
        assert len(events) == 1
        assert events[0].kwargs["user_id"] == "admin"
        assert events[0].kwargs["auth_path"] == "jwt"
        assert "admin" in events[0].kwargs["roles"]

    def test_failed_password_login_writes_no_success_event(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
            )

        assert resp.status_code == 401
        assert _login_successes(mock_logger) == []

    def test_oidc_callback_writes_one_event(self, client: TestClient, app):
        app.state.oidc_rp = _CallbackRp()
        app.state.group_store.update("dfe-admins", source_id="dfe-admins")

        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get("/api/v1/auth/oidc/stub/callback", follow_redirects=False)

        assert resp.status_code == 200
        events = _login_successes(mock_logger)
        assert len(events) == 1
        assert events[0].kwargs["user_id"] == "alice@example.com"
        assert events[0].kwargs["auth_path"] == "oidc"
        assert "admin" in events[0].kwargs["roles"]


class TestTokenValidationWritesNothing:
    """A credential checked on a request is not a login."""

    def test_jwt_request_writes_no_event(self, client: TestClient, admin_headers: dict):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get("/api/v1/auth/me", headers=admin_headers)

        assert resp.status_code == 200
        assert _login_successes(mock_logger) == []

    def test_repeated_jwt_requests_write_no_events(self, client: TestClient, admin_headers: dict):
        """The reported defect: a page view fanning out to many GETs."""
        with patch(AUDIT_LOGGER) as mock_logger:
            for _ in range(5):
                assert client.get("/api/v1/auth/me", headers=admin_headers).status_code == 200

        assert _login_successes(mock_logger) == []

    def test_api_key_request_writes_no_event(self, client: TestClient, api_key: str):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get("/api/v1/auth/me", headers={"X-API-Key": api_key})

        assert resp.status_code == 200
        assert _login_successes(mock_logger) == []

    def test_oidc_header_request_writes_no_event(self, client: TestClient):
        """Envoy authenticated the user upstream - the engine mints nothing here."""
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get(
                "/api/v1/auth/me",
                headers={
                    "X-Oidc-Subject": "alice@example.com",
                    "X-Oidc-Groups": "dfe-admins",
                },
            )

        assert resp.status_code == 200
        assert _login_successes(mock_logger) == []

    def test_token_refresh_writes_no_event(self, client: TestClient, admin_headers: dict):
        """Refresh re-mints from a token already held, so no credential is exchanged."""
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.post("/api/v1/auth/refresh", headers=admin_headers)

        assert resp.status_code == 200
        assert _login_successes(mock_logger) == []
