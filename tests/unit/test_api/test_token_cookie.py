#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_token_cookie.py
#  Purpose:      The engine token cookie is Secure on the posture the session cookie is
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ``dfe_token`` cookie follows the deployment posture, set and cleared alike.

A browser drops a ``Secure`` cookie set over plain http, so a dev stack without TLS
completed the OIDC login and kept no token. Production keeps ``Secure``, and logout
clears the cookie with the same attributes it was set with.
"""

import secrets

import pytest
from fastapi.testclient import TestClient

from dfe_engine.auth.oidc.rp import NormalizedIdentity

_CALLBACK = "/api/v1/auth/oidc/stub/callback"
_LOGIN = "/api/v1/auth/login"
_LOGOUT = "/api/v1/auth/logout"


class _CallbackRp:
    """RP stand-in whose callback returns a fixed identity, so no IdP is needed."""

    def has_provider(self, name: str) -> bool:
        return name == "stub"

    async def handle_callback(self, provider: str, request) -> NormalizedIdentity:
        return NormalizedIdentity(
            subject="alice@example.com", email="alice@example.com", groups=["dfe-admins"]
        )


def _posture(app, env: str) -> None:
    app.state.settings = app.state.settings.model_copy(update={"env": env})


def _cookie_header(resp) -> str:
    return next(v for k, v in resp.headers.items() if k == "set-cookie" and "dfe_token" in v)


def _attributes(header: str) -> set[str]:
    """The cookie's attribute names, lowercased, without the name=value pair."""
    return {part.split("=", 1)[0].strip().lower() for part in header.split(";")[1:]}


@pytest.fixture
def stub_rp(app) -> None:
    app.state.oidc_rp = _CallbackRp()
    app.state.group_store.update("dfe-admins", source_id="dfe-admins")


@pytest.fixture
def bob(app) -> str:
    password = secrets.token_urlsafe(16)
    app.state.account_store.create("bob", password, groups=["dfe-analysts"])
    app.state.group_store.add_member("dfe-analysts", "bob")
    return password


def _bearer(client: TestClient, password: str) -> dict[str, str]:
    resp = client.post(_LOGIN, json={"username": "bob", "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestTheCallbackCookie:
    def test_production_sets_it_secure(self, client: TestClient, stub_rp):
        resp = client.get(_CALLBACK, follow_redirects=False)

        assert resp.status_code == 200, resp.text
        assert "secure" in _attributes(_cookie_header(resp))

    @pytest.mark.parametrize("env", ["dev", "local", "test"])
    def test_a_dev_posture_sets_it_without_secure(self, client: TestClient, app, stub_rp, env):
        _posture(app, env)

        resp = client.get(_CALLBACK, follow_redirects=False)

        assert resp.status_code == 200, resp.text
        attributes = _attributes(_cookie_header(resp))
        assert "secure" not in attributes
        assert "httponly" in attributes


class TestTheLogoutCookie:
    def test_production_clears_it_secure(self, client: TestClient, bob):
        resp = client.post(_LOGOUT, headers=_bearer(client, bob))

        assert resp.status_code == 204, resp.text
        assert "secure" in _attributes(_cookie_header(resp))

    def test_a_dev_posture_clears_it_without_secure(self, client: TestClient, app, bob):
        headers = _bearer(client, bob)
        _posture(app, "dev")

        resp = client.post(_LOGOUT, headers=headers)

        assert resp.status_code == 204, resp.text
        header = _cookie_header(resp)
        assert "Max-Age=0" in header
        assert "secure" not in _attributes(header)
