#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_links.py
#  Purpose:      API tests for accounts that IdP sign-ins are linked to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.settings import DFESettings
from tests.support.accounts import session_token
from tests.support.oidc import serve_provider
from tests.unit.test_api.account_links_cases import PROVIDER_TYPES_CASES, ProviderTypesCase
from tests.unit.test_auth.factories import make_link_ticket

_ACCOUNTS = "/api/v1/auth/accounts"
_EMAIL = "kaz@example.test"
_KEY = "stub-kaz"


@pytest.fixture
def linked(app: FastAPI, client: TestClient) -> FastAPI:
    """Store the local kaz with the stub sign-in stub-kaz linked to it, its g1 giving data_viewer."""
    serve_provider(app)
    app.state.group_store.create(
        "stub-viewers", ["data_viewer"], source_provider="stub", source_id="g1"
    )
    app.state.account_store.create("kaz", "kaz-local-password-1", email=_EMAIL)
    ticket = make_link_ticket(
        account="kaz", email=_EMAIL, groups=["g1"], provider="stub", subject=_KEY
    )
    app.state.jit_provisioner.complete_link(ticket=ticket)
    return app


def _provider_types(*, body: dict[str, Any]) -> tuple[str, list[str]]:
    """An account read's provider type, then its linked sign-ins' types."""
    return (body["oidc_type"], [identity["provider_type"] for identity in body["identities"]])


def _usernames(*, client: TestClient, headers: dict[str, str]) -> list[str]:
    """The account names the list answers with."""
    page = client.get(_ACCOUNTS, headers=headers, params={"limit": 100}).json()
    return sorted(item["username"] for item in page["items"])


class TestReads:
    def test_the_list_hides_link_records(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        assert _KEY not in _usernames(client=client, headers=admin_headers)

    def test_a_link_record_answers_404(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        resp = client.get(f"{_ACCOUNTS}/{_KEY}", headers=admin_headers)
        assert (resp.status_code, resp.json()["code"]) == (404, "not_found")

    def test_the_account_lists_its_sign_ins(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        body = client.get(f"{_ACCOUNTS}/kaz", headers=admin_headers).json()
        identities = [
            (i["key"], i["provider"], i["subject"], i["email"]) for i in body["identities"]
        ]
        assert identities == [(_KEY, "stub", _KEY, _EMAIL)]

    @pytest.mark.parametrize(
        "case", PROVIDER_TYPES_CASES, ids=[case["id"] for case in PROVIDER_TYPES_CASES]
    )
    def test_provider_types(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        linked: FastAPI,
        case: ProviderTypesCase,
    ):
        linked.state.account_store.update(username="kaz", **case["updates"])
        detail = client.get(f"{_ACCOUNTS}/kaz", headers=admin_headers).json()
        page = client.get(_ACCOUNTS, headers=admin_headers, params={"limit": 100}).json()
        listed = next(item for item in page["items"] if item["username"] == "kaz")
        expected = (case["expected_oidc_type"], case["expected_identity_types"])
        assert (_provider_types(body=detail), _provider_types(body=listed)) == (
            expected,
            expected,
        )


class TestUnlink:
    def test_the_sign_in_becomes_its_own_account(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        resp = client.delete(f"{_ACCOUNTS}/kaz/identities/{_KEY}", headers=admin_headers)
        store = linked.state.account_store
        assert (resp.status_code, store.get("kaz").identities, store.get(_KEY).linked_to) == (
            204,
            [],
            "",
        )

    def test_every_session_of_the_account_ends(
        self,
        admin_headers: dict[str, str],
        api_settings: DFESettings,
        client: TestClient,
        linked: FastAPI,
    ):
        token = session_token(linked, api_settings, {"sub": "kaz"})
        client.delete(f"{_ACCOUNTS}/kaz/identities/{_KEY}", headers=admin_headers)
        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert (me.status_code, me.json()["code"]) == (401, "session_ended")

    def test_an_unknown_sign_in_answers_404(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        resp = client.delete(f"{_ACCOUNTS}/kaz/identities/no-such-key", headers=admin_headers)
        assert (resp.status_code, resp.json()["code"]) == (404, "not_found")


class TestDeletes:
    def test_deleting_the_account_deletes_its_link_records(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        resp = client.delete(f"{_ACCOUNTS}/kaz", headers=admin_headers)
        assert (resp.status_code, linked.state.account_store.get(_KEY)) == (204, None)

    def test_deleting_the_provider_unlinks_and_keeps_the_account(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        resp = client.delete("/api/v1/auth/oidc-providers/stub", headers=admin_headers)
        store = linked.state.account_store
        kaz = store.get("kaz")
        assert (resp.status_code, kaz.enabled, kaz.identities, store.get(_KEY)) == (
            200,
            True,
            [],
            None,
        )


class TestEmailTaken:
    def test_a_create_is_refused(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        body = {
            "name": "Test User",
            "email": _EMAIL.upper(),
            "groups": ["dfe-viewers"],
            "password": "another-password-1",
            "username": "kaz2",
        }
        resp = client.post(_ACCOUNTS, headers=admin_headers, json=body)
        assert (resp.status_code, resp.json()["code"]) == (409, "email_taken")

    def test_an_update_is_refused(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        linked.state.account_store.create("kaz2", "another-password-1", email="k2@example.test")
        resp = client.put(f"{_ACCOUNTS}/kaz2", headers=admin_headers, json={"email": _EMAIL})
        assert (resp.status_code, resp.json()["code"]) == (409, "email_taken")

    def test_an_admin_set_email_is_vouched_for(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        body = {
            "name": "Test User",
            "email": "new@example.test",
            "groups": ["dfe-viewers"],
            "password": "another-password-1",
            "username": "kaz3",
        }
        resp = client.post(_ACCOUNTS, headers=admin_headers, json=body)
        assert (resp.status_code, resp.json()["email_verified"]) == (201, True)

    def test_an_own_email_edit_is_not_vouched_for(
        self, api_settings: DFESettings, client: TestClient, linked: FastAPI
    ):
        token = session_token(linked, api_settings, {"sub": "kaz"})
        resp = client.put(
            f"{_ACCOUNTS}/me",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "kaz-new@example.test"},
        )
        assert (resp.status_code, resp.json()["email_verified"]) == (200, False)

    def test_the_holder_keeps_its_own_email(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        resp = client.put(f"{_ACCOUNTS}/kaz", headers=admin_headers, json={"email": _EMAIL})
        assert resp.status_code == 200


class TestAnIdpAdminCannotDeactivateItself:
    @pytest.mark.parametrize(
        ("method", "body", "code"),
        [("PUT", {"enabled": False}, "self_deactivate"), ("DELETE", None, "self_delete")],
    )
    def test_refused(
        self,
        api_settings: DFESettings,
        app: FastAPI,
        client: TestClient,
        method: str,
        body: dict[str, bool] | None,
        code: str,
    ):
        serve_provider(app)
        app.state.group_store.create(
            "stub-admins", ["admin"], source_provider="stub", source_id="g-admin"
        )
        app.state.jit_provisioner.ensure_account(
            "Jane@Corp.com", ["g-admin"], "stub", email="jane@corp.com"
        )
        token = session_token(app, api_settings, {"sub": "Jane@Corp.com"})
        resp = client.request(
            method,
            f"{_ACCOUNTS}/jane-corp-com",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
        assert (resp.status_code, resp.json()["code"]) == (403, code)


class TestScim:
    def test_users_leave_out_link_records(
        self, admin_headers: dict[str, str], client: TestClient, linked: FastAPI
    ):
        listed = client.get("/api/v1/scim/v2/Users", headers=admin_headers).json()
        names = [user["userName"] for user in listed["Resources"]]
        one = client.get(f"/api/v1/scim/v2/Users/{_KEY}", headers=admin_headers)
        assert (_KEY in names, "kaz" in names, one.status_code) == (False, True, 404)
