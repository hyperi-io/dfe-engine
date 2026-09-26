#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_password_change_gate.py
#  Purpose:      The bootstrap admin must replace its issued password before the API serves it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The forced change at first login, over the real app.

The ``client`` fixture in this directory replaces the admin's password to make
every other API test an owner's session, so these tests enter the lifespan
themselves and meet the admin as a fresh deployment leaves it.
"""

import secrets
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.api.metrics import PASSWORD_CHANGE_REFUSALS
from dfe_engine.api.password_change import ALLOWED_BEFORE_CHANGE, PASSWORD_CHANGE_CLAIM
from dfe_engine.settings import DFESettings

from .conftest import ADMIN_PASSWORD

_LOGIN = "/api/v1/auth/login"
_CHANGE = "/api/v1/auth/accounts/reset-password"
_OWN_PASSWORD = f"own-{secrets.token_urlsafe(16)}"


@pytest.fixture
def fresh(api_settings: DFESettings) -> Iterator[TestClient]:
    """The app as a deployment boots it: the admin on its issued password."""
    app = create_app(settings=api_settings)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        _registries.clear()


def _login(client: TestClient, password: str = ADMIN_PASSWORD) -> dict:
    resp = client.post(_LOGIN, json={"username": "admin", "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def _change(client: TestClient, headers: dict[str, str]) -> None:
    resp = client.post(_CHANGE, json={"new_password": _OWN_PASSWORD}, headers=headers)
    assert resp.status_code == 200, resp.text


class TestTheIssuedAdmin:
    def test_login_succeeds_and_says_a_change_is_required(self, fresh):
        body = _login(fresh)

        assert body["password_change_required"] is True
        assert body["access_token"]

    def test_a_read_is_refused_with_the_named_code(self, fresh):
        resp = fresh.get("/api/v1/sources", headers=_bearer(_login(fresh)))

        assert resp.status_code == 403
        assert resp.json()["code"] == "password_change_required"
        assert _CHANGE in resp.json()["message"]

    def test_a_write_is_refused_before_it_touches_anything(self, fresh):
        resp = fresh.post(
            "/api/v1/auth/accounts",
            json={"username": "mallory", "password": secrets.token_urlsafe(16)},
            headers=_bearer(_login(fresh)),
        )

        assert resp.status_code == 403
        assert resp.json()["code"] == "password_change_required"
        assert fresh.app.state.account_store.get("mallory") is None

    def test_the_routes_the_change_needs_still_answer(self, fresh):
        headers = _bearer(_login(fresh))

        me = fresh.get("/api/v1/auth/me", headers=headers)
        own = fresh.get("/api/v1/auth/accounts/me", headers=headers)
        refreshed = fresh.post("/api/v1/auth/refresh", headers=headers)

        assert me.status_code == 200, me.text
        assert me.json()["password_change_required"] is True
        assert own.status_code == 200, own.text
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["password_change_required"] is True

    def test_the_setup_status_is_public_and_unaffected(self, fresh):
        assert fresh.get("/api/v1/auth/setup-status").status_code == 200

    def test_the_token_carries_no_standing_a_peer_could_honour(self, fresh):
        """dfe-hyperdx admits a token by its claims, not by the engine's account gate."""
        body = _login(fresh)
        claims = fresh.app.state.jwt_authority.verify(body["access_token"])

        assert body["roles"] == []
        assert claims["roles"] == []
        assert claims["groups"] == []
        assert claims["org_ids"] == []
        assert "org_id" not in claims
        assert claims["role"] == "member"

    def test_the_token_names_the_pending_change_for_a_peer_to_refuse(self, fresh):
        """dfe-hyperdx puts a claim-less token in its default team, so it refuses on this claim."""
        headers = _bearer(_login(fresh))
        refreshed = fresh.post("/api/v1/auth/refresh", headers=headers).json()

        for token in (headers["Authorization"].removeprefix("Bearer "), refreshed["access_token"]):
            claims = fresh.app.state.jwt_authority.verify(token)
            assert claims[PASSWORD_CHANGE_CLAIM] is True


class TestTheChangedAccount:
    def test_a_refresh_after_the_change_carries_the_accounts_standing(self, fresh):
        headers = _bearer(_login(fresh))
        _change(fresh, headers)

        refreshed = fresh.post("/api/v1/auth/refresh", headers=headers).json()
        claims = fresh.app.state.jwt_authority.verify(refreshed["access_token"])

        assert refreshed["password_change_required"] is False
        assert "admin" in refreshed["roles"]
        assert "dfe-admins" in claims["groups"]
        assert claims["role"] == "admin"
        assert PASSWORD_CHANGE_CLAIM not in claims


class TestTheChange:
    def test_changing_the_password_clears_the_flag(self, fresh):
        headers = _bearer(_login(fresh))

        _change(fresh, headers)

        assert fresh.get("/api/v1/sources", headers=headers).status_code == 200
        assert (
            fresh.get("/api/v1/auth/me", headers=headers).json()["password_change_required"]
            is False
        )
        assert _login(fresh, _OWN_PASSWORD)["password_change_required"] is False

    def test_the_issued_password_stops_working(self, fresh):
        _change(fresh, _bearer(_login(fresh)))

        resp = fresh.post(_LOGIN, json={"username": "admin", "password": ADMIN_PASSWORD})

        assert resp.status_code == 401

    def test_the_issued_password_is_refused_as_the_new_one(self, fresh):
        headers = _bearer(_login(fresh))

        resp = fresh.post(_CHANGE, json={"new_password": ADMIN_PASSWORD}, headers=headers)

        assert resp.status_code == 400
        assert resp.json()["code"] == "password_reused"
        assert fresh.get("/api/v1/sources", headers=headers).status_code == 403


class TestAcrossARestart:
    def test_the_flag_survives_a_restart(self, api_settings):
        for _ in range(2):
            app = create_app(settings=api_settings)
            try:
                with TestClient(app, raise_server_exceptions=False) as client:
                    assert _login(client)["password_change_required"] is True
            finally:
                _registries.clear()

    def test_the_owners_password_survives_a_restart_and_the_flag_stays_clear(self, api_settings):
        app = create_app(settings=api_settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _change(client, _bearer(_login(client)))
        finally:
            _registries.clear()

        app = create_app(settings=api_settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                body = _login(client, _OWN_PASSWORD)
                assert body["password_change_required"] is False
                assert client.get("/api/v1/sources", headers=_bearer(body)).status_code == 200
        finally:
            _registries.clear()


class TestOnlyTheIssuedAccount:
    def test_an_account_on_its_own_password_is_not_gated(self, fresh):
        fresh.app.state.account_store.create("alice", _OWN_PASSWORD, groups=["dfe-admins"])
        fresh.app.state.group_store.add_member("dfe-admins", "alice")

        resp = fresh.post(_LOGIN, json={"username": "alice", "password": _OWN_PASSWORD})

        assert resp.json()["password_change_required"] is False
        assert fresh.get("/api/v1/sources", headers=_bearer(resp.json())).status_code == 200


def _sample(exposition: str, name: str, labels: dict[str, str]) -> float | None:
    for family in text_string_to_metric_families(exposition):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None


def test_a_refusal_is_counted_by_method_and_area(api_settings):
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    app = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            headers = _bearer(_login(client))
            client.get("/api/v1/sources", headers=headers)
            client.get("/api/v1/sources", headers=headers)
            client.get("/api/v1/auth/me", headers=headers)
    finally:
        _registries.clear()

    labels = {"method": "GET", "area": "sources"}
    assert _sample(manager.metrics_text, PASSWORD_CHANGE_REFUSALS, labels) == 2
    assert (
        _sample(manager.metrics_text, PASSWORD_CHANGE_REFUSALS, {"method": "GET", "area": "auth"})
        is None
    )


def test_every_allowed_route_is_a_real_route(app):
    """A renamed route would silently lock the issued admin out of its own change."""
    served: set[tuple[str, str]] = set()
    for path, operations in app.openapi()["paths"].items():
        served.update((method.upper(), path) for method in operations)

    assert ALLOWED_BEFORE_CHANGE <= served
