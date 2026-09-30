#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_forwarded_proto.py
#  Purpose:      The OIDC redirect_uri keeps the scheme a trusted gateway forwarded
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Forwarded-proto trust on the OIDC login path.

``request.url_for`` builds the OIDC ``redirect_uri`` from the request scheme, so
behind a TLS-terminating gateway that scheme has to come from X-Forwarded-Proto
or the IdP is handed an ``http`` callback. create_app installs
``ForwardedHeadersMiddleware`` keyed on ``api.forwarded_allow_ips``, and these tests
drive it through the real app with a non-loopback peer address - the shape the
gateway pod presents.

X-Forwarded-Host is not consulted: the host comes from the Host header, which a
gateway forwards unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import DFESettings

# The gateway pod's address - not loopback, so the default setting must not trust it.
_GATEWAY_PEER = ("198.18.2.46", 51234)

_PROXIED_HEADERS = {
    "Host": "dfe.example.test",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "dfe.example.test",
    "X-Forwarded-For": "203.0.113.9",
}


class _EchoRedirectUriRp:
    """RP stub returning the redirect_uri the router built, in place of an IdP URL."""

    def has_provider(self, name: str) -> bool:
        return name == "stub"

    async def login_authorization_url(self, provider: str, request, redirect_uri: str) -> str:
        return redirect_uri


@contextmanager
def _proxied_client(api_settings: DFESettings, forwarded_allow_ips: str) -> Iterator[TestClient]:
    """A TestClient whose peer is the gateway address, over the given trust setting."""
    settings = api_settings.model_copy(
        update={
            "api": api_settings.api.model_copy(update={"forwarded_allow_ips": forwarded_allow_ips})
        }
    )
    app = create_app(settings=settings)
    try:
        with TestClient(app, client=_GATEWAY_PEER) as client:
            app.state.oidc_rp = _EchoRedirectUriRp()
            yield client
    finally:
        _registries.clear()


def _redirect_uri(client: TestClient) -> str:
    resp = client.get("/api/v1/auth/oidc/stub/login?redirect=false", headers=_PROXIED_HEADERS)
    assert resp.status_code == 200
    return resp.json()["authorization_url"]


def test_trusted_proxy_keeps_the_forwarded_https_scheme(api_settings: DFESettings):
    """With the gateway trusted, the callback URI carries the https the caller used."""
    with _proxied_client(api_settings, "*") as client:
        assert _redirect_uri(client) == "https://dfe.example.test/api/v1/auth/oidc/stub/callback"


def test_trusting_the_gateway_network_is_enough(api_settings: DFESettings):
    """A CIDR covering the gateway pod trusts it without trusting every peer."""
    with _proxied_client(api_settings, "198.18.0.0/15") as client:
        assert _redirect_uri(client).startswith("https://")


def test_default_does_not_trust_a_forwarded_proto_from_a_non_loopback_peer(
    api_settings: DFESettings,
):
    """Loopback-only default: an unfronted engine ignores a spoofable header."""
    with _proxied_client(api_settings, "127.0.0.1") as client:
        assert _redirect_uri(client) == "http://dfe.example.test/api/v1/auth/oidc/stub/callback"


@pytest.mark.parametrize("trusted", ["*", "127.0.0.1"])
def test_login_still_404s_for_an_unregistered_provider(api_settings: DFESettings, trusted: str):
    """Trusting a proxy changes the scheme only - it is not a route-level bypass."""
    with _proxied_client(api_settings, trusted) as client:
        resp = client.get(
            "/api/v1/auth/oidc/nonexistent/login",
            headers=_PROXIED_HEADERS,
            follow_redirects=False,
        )
        assert resp.status_code == 404
