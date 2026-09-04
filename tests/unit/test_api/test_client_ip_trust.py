#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_client_ip_trust.py
#  Purpose:      X-Forwarded-For reaches the audit trail only behind a trusted proxy
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Which address an audit event records.

`client_ip` is only worth reading if a caller cannot choose it. X-Forwarded-For
is caller-supplied unless a trusted proxy rewrites it, so it is read only when
`auth.trust_proxy_auth_headers` says one is in front - the same gate the
X-Oidc-* identity headers sit behind (issue #263).

Patching the audit logger IS the assertion here: these tests are about the
field values a log event carries, the same exception to the no-mocks policy
that tests/unit/test_auth/test_audit.py already takes.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dfe_engine.settings import DFESettings

AUDIT_LOGGER = "dfe_engine.auth.audit.logger"
SPOOFED = "203.0.113.9"


def _denied_client_ip(mock_logger) -> str | None:
    """Return the client_ip on the single auth.login.denied event emitted."""
    events = [
        call
        for call in mock_logger.warning.call_args_list
        if call.args and call.args[0] == "auth.login.denied"
    ]
    assert len(events) == 1
    return events[0].kwargs["client_ip"]


@pytest.fixture
def untrusted_client(api_settings: DFESettings):
    """A client for an engine with no trusted proxy in front (the default)."""
    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries

    untrusted = api_settings.model_copy(
        update={"auth": api_settings.auth.model_copy(update={"trust_proxy_auth_headers": False})}
    )
    application = create_app(settings=untrusted)
    try:
        with TestClient(application, raise_server_exceptions=False) as c:
            yield c
    finally:
        _registries.clear()


class TestAuditedClientIp:
    """An unauthenticated request is audited, so it carries the address."""

    def test_forwarded_header_is_read_behind_a_trusted_proxy(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get("/api/v1/auth/me", headers={"X-Forwarded-For": SPOOFED})

        assert resp.status_code == 401
        assert _denied_client_ip(mock_logger) == SPOOFED

    def test_forwarded_header_is_ignored_without_one(self, untrusted_client: TestClient):
        """The spoof must not reach the audit log on an unfronted engine."""
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = untrusted_client.get("/api/v1/auth/me", headers={"X-Forwarded-For": SPOOFED})

        assert resp.status_code == 401
        assert _denied_client_ip(mock_logger) == "testclient"

    def test_leftmost_hop_is_taken_behind_a_trusted_proxy(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get(
                "/api/v1/auth/me",
                headers={"X-Forwarded-For": f"{SPOOFED}, 198.51.100.7"},
            )

        assert resp.status_code == 401
        assert _denied_client_ip(mock_logger) == SPOOFED

    def test_socket_address_is_used_when_no_header_is_sent(self, client: TestClient):
        with patch(AUDIT_LOGGER) as mock_logger:
            resp = client.get("/api/v1/auth/me")

        assert resp.status_code == 401
        assert _denied_client_ip(mock_logger) == "testclient"
