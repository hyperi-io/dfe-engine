#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sessions.py
#  Purpose:      An engine token dies with its session: logout, password, access and age
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every way a session ends, over the real app.

A token is a bearer credential the client holds, so these end it on the account: a
logout, a password change, the account being disabled and enabled again, and the
account being deleted and created again under its name. A refresh cannot carry a
session past ``api.max_session_minutes`` from its sign-in.
"""

import secrets
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.sessions import AUTH_TIME_CLAIM, SESSION_CLAIM

_LOGIN = "/api/v1/auth/login"
_LOGOUT = "/api/v1/auth/logout"
_REFRESH = "/api/v1/auth/refresh"
_ME = "/api/v1/auth/me"


def _account(app, name: str, password: str) -> None:
    app.state.account_store.create(name, password, groups=["dfe-analysts"])
    app.state.group_store.add_member("dfe-analysts", name)


def _login(client: TestClient, name: str, password: str) -> dict:
    resp = client.post(_LOGIN, json={"username": name, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def _assert_ended(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.get(_ME, headers=headers)
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "session_ended"


@pytest.fixture
def alice(client, app) -> str:
    password = secrets.token_urlsafe(16)
    _account(app, "alice", password)
    return password


class TestTheToken:
    def test_a_login_token_carries_the_session_and_its_sign_in_time(self, client, app, alice):
        before = int(time.time())
        body = _login(client, "alice", alice)
        claims = app.state.jwt_authority.verify(body["access_token"])

        assert claims[SESSION_CLAIM] == app.state.account_store.get("alice").session_marker()
        assert before <= claims[AUTH_TIME_CLAIM] <= int(time.time())
        assert client.get(_ME, headers=_bearer(body)).status_code == 200


class TestLogout:
    def test_logout_ends_every_session_of_the_account(self, client, alice):
        first = _bearer(_login(client, "alice", alice))
        second = _bearer(_login(client, "alice", alice))

        resp = client.post(_LOGOUT, headers=first)

        assert resp.status_code == 204, resp.text
        _assert_ended(client, first)
        _assert_ended(client, second)
        assert client.post(_REFRESH, headers=second).status_code == 401

    def test_a_new_sign_in_after_logout_works(self, client, alice):
        client.post(_LOGOUT, headers=_bearer(_login(client, "alice", alice)))

        fresh = _bearer(_login(client, "alice", alice))

        assert client.get(_ME, headers=fresh).status_code == 200

    def test_logout_clears_the_login_cookie(self, client, alice):
        resp = client.post(_LOGOUT, headers=_bearer(_login(client, "alice", alice)))

        assert 'dfe_token=""' in resp.headers["set-cookie"]
        assert "Max-Age=0" in resp.headers["set-cookie"]

    def test_logout_needs_a_session(self, client):
        assert client.post(_LOGOUT).status_code == 401

    def test_another_accounts_sessions_are_untouched(self, client, app, alice):
        bob_password = secrets.token_urlsafe(16)
        _account(app, "bob", bob_password)
        bob = _bearer(_login(client, "bob", bob_password))

        client.post(_LOGOUT, headers=_bearer(_login(client, "alice", alice)))

        assert client.get(_ME, headers=bob).status_code == 200


class TestPasswordChange:
    def test_the_owners_change_ends_the_sessions_minted_before_it(self, client, alice):
        other_device = _bearer(_login(client, "alice", alice))
        this_device = _bearer(_login(client, "alice", alice))
        new_password = secrets.token_urlsafe(16)

        resp = client.post(
            "/api/v1/auth/accounts/reset-password",
            json={"current_password": alice, "new_password": new_password},
            headers=this_device,
        )

        assert resp.status_code == 200, resp.text
        _assert_ended(client, other_device)
        _assert_ended(client, this_device)
        assert (
            client.get(_ME, headers=_bearer(_login(client, "alice", new_password))).status_code
            == 200
        )

    def test_an_admin_reset_ends_the_accounts_sessions(self, client, alice, admin_headers):
        held = _bearer(_login(client, "alice", alice))

        resp = client.post(
            "/api/v1/auth/accounts/alice/reset-password",
            json={"new_password": secrets.token_urlsafe(16)},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        _assert_ended(client, held)


class TestAccountAccess:
    def test_re_enabling_an_account_does_not_revive_its_old_tokens(
        self, client, alice, admin_headers
    ):
        held = _bearer(_login(client, "alice", alice))

        disabled = client.put(
            "/api/v1/auth/accounts/alice", json={"enabled": False}, headers=admin_headers
        )
        refused = client.get(_ME, headers=held)
        enabled = client.put(
            "/api/v1/auth/accounts/alice", json={"enabled": True}, headers=admin_headers
        )

        assert (disabled.status_code, refused.status_code, enabled.status_code) == (200, 401, 200)
        _assert_ended(client, held)
        assert client.get(_ME, headers=_bearer(_login(client, "alice", alice))).status_code == 200

    def test_unblocking_does_not_revive_old_tokens(self, client, alice, admin_headers):
        held = _bearer(_login(client, "alice", alice))

        client.put("/api/v1/auth/accounts/alice", json={"blocked": True}, headers=admin_headers)
        client.put("/api/v1/auth/accounts/alice", json={"blocked": False}, headers=admin_headers)

        _assert_ended(client, held)

    def test_an_edit_that_leaves_access_alone_keeps_the_session(self, client, alice, admin_headers):
        held = _bearer(_login(client, "alice", alice))

        resp = client.put(
            "/api/v1/auth/accounts/alice",
            json={"enabled": True, "name": "Alice A"},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert client.get(_ME, headers=held).status_code == 200

    def test_a_recreated_username_does_not_inherit_the_old_tokens(
        self, client, app, alice, admin_headers
    ):
        held = _bearer(_login(client, "alice", alice))

        deleted = client.delete("/api/v1/auth/accounts/alice", headers=admin_headers)
        _account(app, "alice", alice)

        assert deleted.status_code == 204, deleted.text
        _assert_ended(client, held)


class TestTokensFromBeforeTheClaim:
    """A token minted before tokens carried a marker lives out its expiry, no further."""

    def test_it_is_accepted_while_the_account_has_never_ended_a_session(
        self, client, api_settings, alice
    ):
        legacy = create_access_token(data={"sub": "alice"}, settings=api_settings)

        resp = client.get(_ME, headers={"Authorization": f"Bearer {legacy}"})

        assert resp.status_code == 200, resp.text

    def test_it_is_refused_once_the_account_logs_out(self, client, api_settings, alice):
        legacy = {
            "Authorization": "Bearer "
            + create_access_token(data={"sub": "alice"}, settings=api_settings)
        }

        client.post(_LOGOUT, headers=_bearer(_login(client, "alice", alice)))

        _assert_ended(client, legacy)


class TestMaximumSessionAge:
    def _token(self, app, api_settings, *, signed_in_ago: int) -> dict[str, str]:
        marker = app.state.account_store.get("alice").session_marker()
        token = create_access_token(
            data={
                "sub": "alice",
                SESSION_CLAIM: marker,
                AUTH_TIME_CLAIM: int(time.time()) - signed_in_ago,
            },
            settings=api_settings,
            expires_delta=timedelta(minutes=5),
        )
        return {"Authorization": f"Bearer {token}"}

    def test_a_refresh_keeps_the_sign_in_time(self, client, app, api_settings, alice):
        headers = self._token(app, api_settings, signed_in_ago=600)

        resp = client.post(_REFRESH, headers=headers)
        claims = app.state.jwt_authority.verify(resp.json()["access_token"])

        assert resp.status_code == 200, resp.text
        original = app.state.jwt_authority.verify(headers["Authorization"][7:])
        assert claims[AUTH_TIME_CLAIM] == original[AUTH_TIME_CLAIM]

    def test_a_refresh_past_the_maximum_age_is_refused(self, client, app, api_settings, alice):
        too_old = api_settings.api.max_session_minutes * 60 + 1
        headers = self._token(app, api_settings, signed_in_ago=too_old)

        resp = client.post(_REFRESH, headers=headers)

        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "session_expired"

    def test_a_refresh_near_the_maximum_age_mints_a_token_that_ends_with_it(
        self, client, app, api_settings, alice
    ):
        left = 90
        signed_in_ago = api_settings.api.max_session_minutes * 60 - left
        headers = self._token(app, api_settings, signed_in_ago=signed_in_ago)

        resp = client.post(_REFRESH, headers=headers)
        claims = app.state.jwt_authority.verify(resp.json()["access_token"])

        assert resp.status_code == 200, resp.text
        assert resp.json()["expires_in"] <= left
        assert claims["exp"] <= claims[AUTH_TIME_CLAIM] + api_settings.api.max_session_minutes * 60

    def test_a_login_token_never_outlives_the_maximum_age(self, client, app, api_settings, alice):
        api_settings.api.max_session_minutes = 10

        body = _login(client, "alice", alice)
        claims = app.state.jwt_authority.verify(body["access_token"])

        assert body["expires_in"] == 600
        assert claims["exp"] - claims[AUTH_TIME_CLAIM] <= 600

    def test_a_legacy_token_refreshes_from_when_it_was_issued(
        self, client, app, api_settings, alice
    ):
        legacy = create_access_token(data={"sub": "alice"}, settings=api_settings)

        resp = client.post(_REFRESH, headers={"Authorization": f"Bearer {legacy}"})
        claims = app.state.jwt_authority.verify(resp.json()["access_token"])

        assert resp.status_code == 200, resp.text
        assert claims[AUTH_TIME_CLAIM] == app.state.jwt_authority.verify(legacy)["iat"]
