"""Tests for the runtime client-config bootstrap endpoint."""

from dfe_engine.auth.oidc.models import OIDCProvider

CLIENT_CONFIG = "/api/v1/config/client"
FEATURE_FLAGS = "/api/v1/config/features"


def _auth_mode(client, headers) -> str:
    resp = client.get(CLIENT_CONFIG, headers=headers)
    assert resp.status_code == 200
    return resp.json()["auth_mode"]


def test_client_config_refuses_an_anonymous_request(client):
    """The body names HyperDX's in-deployment URL, so a caller with no session gets none of it."""
    resp = client.get(CLIENT_CONFIG)
    assert resp.status_code == 401
    assert "hyperdx" not in resp.json()


def test_client_config_is_shaped_for_any_authenticated_caller(client, viewer_headers):
    # The least-privileged account: reading it needs a session, not a role.
    resp = client.get(CLIENT_CONFIG, headers=viewer_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "hyperdx" in body
    assert "enabled" in body["hyperdx"]
    assert body["auth_mode"] in ("jwt", "oidc")
    assert "governed_ops" in body["features"]
    # gitops disabled in test settings -> governed_ops feature off
    assert body["features"]["governed_ops"] is False


def test_auth_mode_is_jwt_with_no_oidc_provider(client, viewer_headers):
    """No provider registered - the UI must not offer an SSO button."""
    assert _auth_mode(client, viewer_headers) == "jwt"


def test_auth_mode_is_oidc_once_an_enabled_provider_is_registered(client, app, viewer_headers):
    """An enabled provider in the registry is what makes the login route work."""
    app.state.oidc_provider_registry.create(
        "dex", OIDCProvider(type="generic", issuer="https://idp.example")
    )
    assert _auth_mode(client, viewer_headers) == "oidc"


def test_feature_flags_refuse_an_anonymous_request(client):
    resp = client.get(FEATURE_FLAGS)

    assert resp.status_code == 401


def test_feature_flags_are_the_configured_object(client, app, viewer_headers):
    app.state.settings.feature_flags = {"rules_v2": True, "beta_search": False}

    resp = client.get(FEATURE_FLAGS, headers=viewer_headers)

    assert resp.status_code == 200
    assert resp.json() == {"rules_v2": True, "beta_search": False}


def test_feature_flags_are_empty_until_configured(client, viewer_headers):
    resp = client.get(FEATURE_FLAGS, headers=viewer_headers)

    assert resp.status_code == 200
    assert resp.json() == {}


def test_auth_mode_stays_jwt_for_a_disabled_provider(client, app, viewer_headers):
    """A disabled provider serves no login, so it must not be reported as one."""
    app.state.oidc_provider_registry.create(
        "dex", OIDCProvider(type="generic", issuer="https://idp.example", enabled=False)
    )
    assert _auth_mode(client, viewer_headers) == "jwt"
