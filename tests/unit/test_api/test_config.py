"""Tests for the runtime client-config bootstrap endpoint."""

from __future__ import annotations

from dfe_engine.auth.oidc.models import OIDCProvider


def _auth_mode(client) -> str:
    resp = client.get("/api/v1/config/client")
    assert resp.status_code == 200
    return resp.json()["auth_mode"]


def test_client_config_is_public_and_shaped(client):
    # public (no auth) so the UI can load it before login
    resp = client.get("/api/v1/config/client")
    assert resp.status_code == 200
    body = resp.json()
    assert "hyperdx" in body
    assert "enabled" in body["hyperdx"]
    assert body["auth_mode"] in ("jwt", "oidc")
    assert "governed_ops" in body["features"]
    # gitops disabled in test settings -> governed_ops feature off
    assert body["features"]["governed_ops"] is False


def test_auth_mode_is_jwt_with_no_oidc_provider(client):
    """No provider registered - the UI must not offer an SSO button."""
    assert _auth_mode(client) == "jwt"


def test_auth_mode_is_oidc_once_an_enabled_provider_is_registered(client, app):
    """An enabled provider in the registry is what makes the login route work."""
    app.state.oidc_provider_registry.create(
        "dex", OIDCProvider(type="generic", issuer="https://idp.example")
    )
    assert _auth_mode(client) == "oidc"


def test_auth_mode_stays_jwt_for_a_disabled_provider(client, app):
    """A disabled provider serves no login, so it must not be reported as one."""
    app.state.oidc_provider_registry.create(
        "dex", OIDCProvider(type="generic", issuer="https://idp.example", enabled=False)
    )
    assert _auth_mode(client) == "jwt"
