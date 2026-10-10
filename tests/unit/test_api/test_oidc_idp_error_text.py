#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_idp_error_text.py
#  Purpose:      The OIDC provider routes answer an IdP failure without the IdP's own text
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The OIDC provider routes report a failed IdP call by its status, never by the IdP's text.

An IdP client's error text carries the request URL and can echo the response
body, and either can name a user. Each test points a route at a local stand-in
that refuses the call with :data:`SENTINEL` in its body, then checks the response
carries neither the sentinel nor the URL, while the engine log carries the status.
"""

import json
import socket

import pytest
from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build

from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from tests.support.loopback import CountingListener
from tests.support.refusing_api import refusing_api

SENTINEL = "idp.sentinel.7f3a@example.com"
SENTINEL_LOCAL_PART = "idp.sentinel.7f3a"


def _assert_hidden(text: str, base_url: str) -> None:
    """``text`` carries neither the sentinel user nor the URL the IdP client called."""
    assert SENTINEL_LOCAL_PART not in text, text
    assert base_url not in text, text


def _status_logged(events: list[dict], operation: str, status: int) -> bool:
    return any(e.get("operation") == operation and e.get("status") == status for e in events)


class TestConnectionTest:
    """GET /api/v1/auth/oidc-providers/{name}/test"""

    def test_a_refused_directory_probe_answers_without_googles_text(
        self, app, client, admin_headers, audit_events, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("DFE_TEST_IDP_SA_JSON", '{"type": "service_account"}')
        app.state.oidc_provider_registry.create(
            "gw-refused",
            OIDCProvider(
                type="google",
                issuer="https://accounts.google.com",
                groups=GroupResolutionConfig(
                    mode="api", service_account_json_env="DFE_TEST_IDP_SA_JSON"
                ),
            ),
        )
        refusal = {
            "error": {
                "code": 403,
                "errors": [{"reason": "forbidden"}],
                "message": f"Not Authorized to access this resource for {SENTINEL}",
            }
        }
        with refusing_api(403, refusal) as server:
            service = build(
                "admin",
                "directory_v1",
                cache_discovery=False,
                client_options={"api_endpoint": f"{server.base_url}/"},
                credentials=AnonymousCredentials(),
            )
            monkeypatch.setattr(GoogleAdapter, "_get_service", lambda _self: service)
            resp = client.get("/api/v1/auth/oidc-providers/gw-refused/test", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is False
        _assert_hidden(resp.text, server.base_url)
        assert "HTTP 403" in resp.json()["message"]
        assert _status_logged(audit_events, "test_connection", 403)

    def test_a_refused_graph_probe_answers_without_the_url(
        self, app, client, admin_headers, audit_events, monkeypatch: pytest.MonkeyPatch
    ):
        app.state.oidc_provider_registry.create(
            "entra-refused",
            OIDCProvider(
                type="entra_id",
                issuer="https://login.microsoftonline.com/test-tenant/v2.0",
                groups=GroupResolutionConfig(mode="api"),
            ),
        )
        refusal = {"error": {"code": "Authorization_RequestDenied", "message": SENTINEL}}
        with refusing_api(403, refusal) as server:
            monkeypatch.setattr(EntraAdapter, "GRAPH_BASE", server.base_url)
            monkeypatch.setattr(EntraAdapter, "_get_token", lambda _self: "app-token")
            resp = client.get(
                "/api/v1/auth/oidc-providers/entra-refused/test", headers=admin_headers
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is False
        _assert_hidden(resp.text, server.base_url)
        assert "HTTP 403" in resp.json()["message"]
        assert _status_logged(audit_events, "test_connection", 403)


class TestVerifyLogin:
    """GET /api/v1/auth/oidc-providers/{name}/verify-login"""

    def test_a_refused_discovery_document_answers_without_the_url(
        self, app, client, admin_headers, audit_events
    ):
        with refusing_api(404, {"error": "not_found", "error_description": SENTINEL}) as server:
            app.state.oidc_provider_registry.create(
                "vl-refused", OIDCProvider(type="generic", issuer=server.base_url)
            )
            resp = client.get(
                "/api/v1/auth/oidc-providers/vl-refused/verify-login", headers=admin_headers
            )

        assert resp.status_code == 200, resp.text
        discovery = next(c for c in resp.json()["checks"] if c["name"] == "discovery")
        assert discovery["ok"] is False
        assert server.base_url not in discovery["detail"]
        assert SENTINEL_LOCAL_PART not in resp.text
        assert "HTTP 404" in discovery["detail"]
        assert _status_logged(audit_events, "discovery", 404)

    @pytest.mark.parametrize("reachable_over", ["tls", "refused"])
    def test_an_unreachable_issuer_logs_why_and_no_host_beyond_the_issuer(
        self, app, client, admin_headers, audit_events, reachable_over
    ):
        """The answer stays constant, while the log names the socket-level cause by class."""
        listener = CountingListener()
        if reachable_over == "tls":
            # The issuer is https and the listener drops the handshake: a TLS failure.
            scheme, port, expected = "https", listener.port, "tls"
        else:
            scheme, expected = "http", "connection_refused"
            with socket.socket() as spare:
                spare.bind(("127.0.0.1", 0))
                port = spare.getsockname()[1]
        try:
            app.state.oidc_provider_registry.create(
                "vl-unreachable",
                OIDCProvider(
                    type="generic", issuer=f"{scheme}://127.0.0.1:{port}/{SENTINEL_LOCAL_PART}"
                ),
            )
            resp = client.get(
                "/api/v1/auth/oidc-providers/vl-unreachable/verify-login", headers=admin_headers
            )
        finally:
            listener.close()

        discovery = next(c for c in resp.json()["checks"] if c["name"] == "discovery")
        assert discovery["ok"] is False
        assert "could not be reached" in discovery["detail"]
        assert SENTINEL_LOCAL_PART not in resp.text
        (line,) = [e for e in audit_events if e.get("operation") == "discovery"]
        assert line["transport_failure"] == expected
        assert line["error_type"] == "ConnectError"
        assert "status" not in line
        logged = json.dumps(line, default=str)
        assert "127.0.0.1" not in logged
        assert SENTINEL_LOCAL_PART not in logged
