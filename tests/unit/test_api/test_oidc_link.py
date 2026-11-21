#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_link.py
#  Purpose:      API tests for linking an IdP sign-in to a local account by its password
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.auth.identity_links import LINK_TICKET_AUDIENCE
from dfe_engine.auth.oidc.rp import NormalizedIdentity
from tests.support.oidc import serve_provider

_EMAIL = "kaz@example.test"
_PASSWORD = "kaz-local-password-1"
_SUBJECT = "stub-kaz"


class _VerifiedOidcRp:
    """An RP stub whose stub provider vouches for kaz's email."""

    def has_provider(self, name: str) -> bool:
        """Serve the stub provider only."""
        return name == "stub"

    async def handle_callback(self, provider: str, request: object) -> NormalizedIdentity:
        """Answer every callback with kaz's verified identity."""
        return NormalizedIdentity(
            email=_EMAIL, email_verified=True, groups=["g1"], name="Kaz", subject=_SUBJECT
        )

    async def login_authorization_url(
        self, provider: str, request: object, redirect_uri: str
    ) -> str:
        """Hand back a fixed authorize URL."""
        return "https://idp.example/authorize?state=test"


def _callback(*, client: TestClient):
    """Run the stub provider's callback without following its redirect."""
    return client.get("https://testserver/api/v1/auth/oidc/stub/callback", follow_redirects=False)


def _link(*, client: TestClient, password: str, ticket: str):
    """Redeem *ticket* with *password*."""
    return client.post("/api/v1/auth/oidc/link", json={"password": password, "ticket": ticket})


def _ticket(*, client: TestClient) -> str:
    """The ticket a JSON callback answers link_required with."""
    return _callback(client=client).json()["link_ticket"]


@pytest.fixture
def linkable(app: FastAPI, client: TestClient) -> FastAPI:
    """Trust the stub provider for example.test, give its g1 a role and store the local kaz holding the email."""
    serve_provider(app)
    app.state.oidc_provider_registry.update("stub", trusted_email_domains=["example.test"])
    app.state.group_store.create(
        "stub-viewers", ["data_viewer"], source_provider="stub", source_id="g1"
    )
    app.state.account_store.create("kaz", _PASSWORD, email=_EMAIL)
    app.state.oidc_rp = _VerifiedOidcRp()
    return app


class TestCallbackHoldsTheLink:
    def test_json(self, client: TestClient, linkable: FastAPI):
        resp = _callback(client=client)
        body = resp.json()
        assert (resp.status_code, body["code"], body["account"], "link_ticket" in body) == (
            409,
            "link_required",
            "kaz",
            True,
        )

    def test_writes_nothing(self, client: TestClient, linkable: FastAPI):
        _callback(client=client)
        assert linkable.state.account_store.get(_SUBJECT) is None

    def test_browser(self, client: TestClient, linkable: FastAPI):
        client.get(
            "https://testserver/api/v1/auth/oidc/stub/login",
            params={"redirect": "false", "return_to": "/login/oidc?login_nonce=n1"},
        )
        resp = _callback(client=client)
        location = urlsplit(resp.headers["location"])
        fields = parse_qs(location.fragment)
        assert (
            resp.status_code,
            location.path,
            fields["error"],
            fields["account"],
            "link_ticket" in fields,
            "access_token" in fields,
        ) == (303, "/login/oidc", ["link_required"], ["kaz"], True, False)


class TestLink:
    def test_signs_in_as_the_account(self, client: TestClient, linkable: FastAPI):
        resp = _link(client=client, password=_PASSWORD, ticket=_ticket(client=client))
        bearer = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        me = client.get("/api/v1/auth/me", headers=bearer).json()
        assert (resp.status_code, me["user_id"], me["roles"]) == (200, "kaz", ["data_viewer"])

    def test_the_next_login_needs_no_password(self, client: TestClient, linkable: FastAPI):
        _link(client=client, password=_PASSWORD, ticket=_ticket(client=client))
        resp = _callback(client=client)
        bearer = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        me = client.get("/api/v1/auth/me", headers=bearer).json()
        assert (resp.status_code, me["user_id"]) == (200, "kaz")

    def test_the_local_password_keeps_working(self, client: TestClient, linkable: FastAPI):
        _link(client=client, password=_PASSWORD, ticket=_ticket(client=client))
        resp = client.post("/api/v1/auth/login", json={"password": _PASSWORD, "username": "kaz"})
        assert (resp.status_code, resp.json()["roles"]) == (200, ["data_viewer"])

    def test_a_wrong_password_links_nothing(self, client: TestClient, linkable: FastAPI):
        resp = _link(client=client, password="not-the-password", ticket=_ticket(client=client))
        assert (
            resp.status_code,
            resp.json()["code"],
            linkable.state.account_store.get(_SUBJECT),
        ) == (
            401,
            "unauthorized",
            None,
        )

    @pytest.mark.parametrize("ticket", ["not-a-token", "expired", "another-audience"])
    def test_an_unusable_ticket(self, client: TestClient, linkable: FastAPI, ticket: str):
        authority = linkable.state.jwt_authority
        tickets = {
            "another-audience": authority.sign(claims={"aud": "dfe-hyperdx", "account": "kaz"}),
            "expired": authority.sign(
                claims={"aud": LINK_TICKET_AUDIENCE, "account": "kaz"},
                expires_delta=timedelta(minutes=-10),
            ),
            "not-a-token": "not-a-token",
        }
        resp = _link(client=client, password=_PASSWORD, ticket=tickets[ticket])
        assert (resp.status_code, resp.json()["code"]) == (401, "link_ticket_invalid")

    def test_withdrawn_trust_refuses_the_ticket(self, client: TestClient, linkable: FastAPI):
        ticket = _ticket(client=client)
        linkable.state.oidc_provider_registry.update("stub", trusted_email_domains=[])
        resp = _link(client=client, password=_PASSWORD, ticket=ticket)
        assert (resp.status_code, resp.json()["code"]) == (401, "link_stale")

    def test_the_ticket_is_no_bearer_token(self, client: TestClient, linkable: FastAPI):
        bearer = {"Authorization": f"Bearer {_ticket(client=client)}"}
        assert client.get("/api/v1/auth/me", headers=bearer).status_code == 401
