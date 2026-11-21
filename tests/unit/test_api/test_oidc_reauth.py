#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_reauth.py
#  Purpose:      API tests for re-auth through a fresh sign-in at the caller's identity provider
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import jwt_authority_for
from dfe_engine.api.v1 import oidc_login
from dfe_engine.auth.oidc.rp import NormalizedIdentity
from dfe_engine.auth.reauth import grant_allows
from dfe_engine.settings import DFESettings
from tests.support.accounts import session_token
from tests.support.oidc import serve_provider

LOGIN = "https://testserver/api/v1/auth/oidc/stub/login"
CALLBACK = "https://testserver/api/v1/auth/oidc/stub/callback"
REAUTH = "https://testserver/api/v1/auth/oidc/reauth"
RETURN_TO = "/settings/accounts"


class _ReauthRp:
    """A stub RP that hands out a fresh state per authorize request and signs in *subject* at *auth_time*."""

    def __init__(self, *, auth_time: int | None, subject: str) -> None:
        self.auth_time = auth_time
        self.issued = 0
        self.params = {}
        self.subject = subject

    def has_provider(self, name: str) -> bool:
        """Whether *name* is the stub provider."""
        return name == "stub"

    async def handle_callback(self, provider: str, request: object) -> NormalizedIdentity:
        """The identity the stub IdP signs in."""
        return NormalizedIdentity(auth_time=self.auth_time, groups=["g1"], subject=self.subject)

    async def login_authorization_url(
        self, provider: str, request: object, redirect_uri: str, **params: str | int
    ) -> str:
        """Record *params* and answer with an authorize URL carrying the next state."""
        self.issued += 1
        self.params = params
        return f"https://idp.example/authorize?state=s{self.issued}"


def _fragment(*, location: str) -> dict[str, str]:
    """The fields of *location*'s URL fragment."""
    return {key: values[0] for key, values in parse_qs(urlsplit(location).fragment).items()}


def _state(*, url: str) -> str:
    """The ``state`` an authorize URL carries."""
    return parse_qs(urlsplit(url).query)["state"][0]


def _reauth(
    *,
    app: FastAPI,
    client: TestClient,
    headers: dict[str, str],
    kind: str = "account",
    rp: _ReauthRp,
    target: str = "bob",
) -> dict[str, str]:
    """Start a re-auth for *target*, come back through the callback on its state and return the hand-back fragment."""
    app.state.oidc_rp = rp
    body = {"kind": kind, "return_to": RETURN_TO, "target": target}
    started = client.post(REAUTH, headers=headers, json=body)
    assert started.status_code == 200, started.text
    state = _state(url=started.json()["authorization_url"])
    resp = client.get(f"{CALLBACK}?state={state}", follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith(RETURN_TO)
    return _fragment(location=resp.headers["location"])


@pytest.fixture
def ida_headers(api_settings: DFESettings, app: FastAPI, client: TestClient) -> dict[str, str]:
    """A session for ida, who signs in through the stub provider only."""
    serve_provider(app)
    app.state.group_store.create(
        "stub-viewers", ["data_viewer"], source_provider="stub", source_id="g1"
    )
    accounts = app.state.account_store
    accounts.create("ida", "", groups=["stub-viewers"])
    accounts.update("ida", external=True, oidc_id="stub", source_provider="stub", subject="ida")
    token = session_token(app, api_settings, {"sub": "ida"})
    return {"Authorization": f"Bearer {token}"}


class TestOidcReauth:
    def test_asks_the_idp_for_credentials_again(
        self, app: FastAPI, client: TestClient, ida_headers: dict[str, str]
    ):
        rp = _ReauthRp(auth_time=int(time.time()), subject="ida")
        _reauth(app=app, client=client, headers=ida_headers, rp=rp)
        assert rp.params == {"max_age": 0, "prompt": "login"}

    @pytest.mark.parametrize(
        ("kind", "target", "expected_target"),
        [
            ("account", "bob", "bob"),
            ("group", "bob-team", "group:bob-team"),
            ("role", "bob-role", "role:bob-role"),
        ],
        ids=["account", "group", "role"],
    )
    def test_a_fresh_sign_in_by_the_caller_earns_a_grant(
        self,
        api_settings: DFESettings,
        app: FastAPI,
        client: TestClient,
        ida_headers: dict[str, str],
        kind: str,
        target: str,
        expected_target: str,
    ):
        rp = _ReauthRp(auth_time=int(time.time()), subject="ida")
        fields = _reauth(
            app=app, client=client, headers=ida_headers, kind=kind, rp=rp, target=target
        )
        assert grant_allows(
            authority=jwt_authority_for(api_settings),
            caller="ida",
            target=expected_target,
            token=fields["reauth_grant"],
        )

    @pytest.mark.parametrize(
        ("subject", "age", "expected_error"),
        [
            ("someone-else", 0, "reauth_other_person"),
            ("ida", 600, "reauth_not_fresh"),
            ("ida", None, "reauth_not_fresh"),
        ],
        ids=["another_person", "stale_sign_in", "no_auth_time"],
    )
    def test_a_sign_in_that_proves_nothing_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        ida_headers: dict[str, str],
        subject: str,
        age: int | None,
        expected_error: str,
    ):
        auth_time = None if age is None else int(time.time()) - age
        rp = _ReauthRp(auth_time=auth_time, subject=subject)
        fields = _reauth(app=app, client=client, headers=ida_headers, rp=rp)
        assert (fields.get("error"), "reauth_grant" in fields) == (expected_error, False)

    def test_a_slow_sign_in_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        ida_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(oidc_login, "_REAUTH_SIGN_IN_SECONDS", -1)
        rp = _ReauthRp(auth_time=int(time.time()), subject="ida")
        fields = _reauth(app=app, client=client, headers=ida_headers, rp=rp)
        assert fields.get("error") == "reauth_expired"

    def test_a_login_open_in_another_tab_stays_a_login(
        self, app: FastAPI, client: TestClient, ida_headers: dict[str, str]
    ):
        app.state.oidc_rp = _ReauthRp(auth_time=int(time.time()), subject="ida")
        body = {"kind": "account", "return_to": RETURN_TO, "target": "bob"}
        started = client.post(REAUTH, headers=ida_headers, json=body)
        assert started.status_code == 200, started.text
        login = client.get(f"{LOGIN}?redirect=false")
        assert login.status_code == 200, login.text

        resp = client.get(
            f"{CALLBACK}?state={_state(url=login.json()['authorization_url'])}",
            follow_redirects=False,
        )
        assert "reauth_grant" not in resp.text + resp.headers.get("location", "")

        again = client.get(
            f"{CALLBACK}?state={_state(url=started.json()['authorization_url'])}",
            follow_redirects=False,
        )
        assert "reauth_grant" in _fragment(location=again.headers.get("location", "")), again.text

    def test_a_password_account_is_sent_to_its_password(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        body = {"kind": "account", "return_to": RETURN_TO, "target": "bob"}
        resp = client.post(REAUTH, headers=admin_headers, json=body)
        assert (resp.status_code, resp.json()["code"]) == (409, "reauth_via_password")

    def test_an_untrusted_return_to_is_refused(
        self, app: FastAPI, client: TestClient, ida_headers: dict[str, str]
    ):
        app.state.oidc_rp = _ReauthRp(auth_time=int(time.time()), subject="ida")
        body = {"kind": "account", "return_to": "https://evil.example/x", "target": "bob"}
        resp = client.post(REAUTH, headers=ida_headers, json=body)
        assert (resp.status_code, resp.json()["code"]) == (400, "invalid_return_to")
