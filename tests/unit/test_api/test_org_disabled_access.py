#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_disabled_access.py
#  Purpose:      A disabled org blocks its accounts and keys, and enabling it restores them as they were
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from collections.abc import Callable

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
ORG = "/api/v1/orgs/globex"
PASSWORD = "Correct-Horse-Battery-9"


def _api_key(*, app: FastAPI, client: TestClient) -> Callable[[], httpx2.Response]:
    """A request made with an API key bound to globex."""
    _, token = app.state.api_key_store.create("globex-key", groups=["globex-viewers"], org="globex")

    def request() -> httpx2.Response:
        return client.get(ME, headers={"X-API-Key": token})

    return request


def _login(*, client: TestClient) -> httpx2.Response:
    """gina's password sign-in."""
    return client.post(LOGIN, json={"password": PASSWORD, "username": "gina"})


def _password(*, app: FastAPI, client: TestClient) -> Callable[[], httpx2.Response]:
    """gina's password sign-in, repeatable."""

    def request() -> httpx2.Response:
        return _login(client=client)

    return request


def _session(*, app: FastAPI, client: TestClient) -> Callable[[], httpx2.Response]:
    """A request on the session gina signed in to while globex was enabled."""
    token = _login(client=client).json()["access_token"]

    def request() -> httpx2.Response:
        return client.get(ME, headers={"Authorization": f"Bearer {token}"})

    return request


def _set_enabled(*, admin_headers: dict[str, str], client: TestClient, enabled: bool) -> None:
    """Enable or disable globex as an admin."""
    resp = client.put(ORG, headers=admin_headers, json={"enabled": enabled})
    assert resp.status_code == 200, resp.text


ATTEMPTS = {"api_key": _api_key, "password": _password, "session": _session}


@pytest.fixture
def globex(admin_headers: dict[str, str], app: FastAPI, client: TestClient) -> None:
    """globex with gina, a viewer who signs in by password."""
    resp = client.post("/api/v1/orgs", headers=admin_headers, json={"name": "globex"})
    assert resp.status_code == 201, resp.text
    app.state.account_store.create("gina", PASSWORD, groups=["globex-viewers"], org="globex")


class TestDisabledOrg:
    @pytest.mark.parametrize("path", sorted(ATTEMPTS))
    def test_blocks_then_restores(
        self,
        admin_headers: dict[str, str],
        app: FastAPI,
        client: TestClient,
        globex: None,
        path: str,
    ):
        attempt = ATTEMPTS[path](app=app, client=client)
        assert attempt().status_code == 200

        _set_enabled(admin_headers=admin_headers, client=client, enabled=False)
        refused = attempt()
        assert (refused.status_code, refused.json()["code"]) == (401, "org_disabled")

        _set_enabled(admin_headers=admin_headers, client=client, enabled=True)
        assert attempt().status_code == 200

    def test_the_account_list_says_its_org_is_disabled(
        self, admin_headers: dict[str, str], client: TestClient, globex: None
    ):
        def org_enabled() -> bool:
            return client.get("/api/v1/auth/accounts/gina", headers=admin_headers).json()[
                "org_enabled"
            ]

        assert org_enabled() is True
        _set_enabled(admin_headers=admin_headers, client=client, enabled=False)
        assert org_enabled() is False

    def test_an_account_disabled_on_its_own_stays_disabled(
        self, admin_headers: dict[str, str], app: FastAPI, client: TestClient, globex: None
    ):
        app.state.account_store.update("gina", enabled=False)
        _set_enabled(admin_headers=admin_headers, client=client, enabled=False)
        _set_enabled(admin_headers=admin_headers, client=client, enabled=True)

        resp = _login(client=client)
        assert (resp.status_code, resp.json()["detail"]) == (401, "Account disabled")
