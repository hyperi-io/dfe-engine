#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_org_binding.py
#  Purpose:      A local account's session carries the org it was created in, and no other
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A session's ``org_id`` is the org its account is bound to, never a literal.

Every local account used to sign in as org ``default``, and a tenant-isolated view
binds that value as the org it reads. So a session read whichever org happened to
be called ``default``, whatever org its account was created in.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.query.executor import ViewExecutor
from dfe_engine.settings import DFESettings
from tests.unit.test_query.view_fakes import OneViewCatalog, RecordingClient, view_definition

ACCOUNTS = "/api/v1/auth/accounts"
EXECUTE = "/api/v1/queries/views/analytics/events/execute"
PASSWORD = "pw-alice-123456"


@pytest.fixture
def orgs(client: TestClient, admin_headers: dict[str, str]) -> None:
    for org in ("acme", "globex"):
        resp = client.post("/api/v1/orgs", json={"name": org}, headers=admin_headers)
        assert resp.status_code == 201, resp.text


def _sign_in(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create(client: TestClient, admin_headers: dict[str, str], **fields):
    body = {"username": "alice", "password": PASSWORD, **fields}
    return client.post(ACCOUNTS, json=body, headers=admin_headers)


def _session_org(client: TestClient, headers: dict[str, str]) -> str:
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["org_id"]


class TestALocalAccountIsBoundToItsOrg:
    def test_it_signs_in_bound_to_the_org_it_was_created_in(
        self, client: TestClient, admin_headers: dict[str, str], orgs: None
    ):
        created = _create(client, admin_headers, org="acme")

        assert created.status_code == 201, created.text
        assert created.json()["org"] == "acme"
        assert _session_org(client, _sign_in(client, "alice")) == "acme"

    def test_an_account_bound_to_no_org_signs_in_with_none(
        self, client: TestClient, admin_headers: dict[str, str]
    ):
        assert _create(client, admin_headers).status_code == 201

        assert _session_org(client, _sign_in(client, "alice")) == ""

    def test_an_org_the_registry_does_not_hold_is_refused(
        self, app: FastAPI, client: TestClient, admin_headers: dict[str, str], orgs: None
    ):
        resp = _create(client, admin_headers, org="initech")

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "unknown_org"
        assert app.state.account_store.get("alice") is None

    def test_the_session_org_comes_from_the_account_not_the_token(
        self,
        api_settings: DFESettings,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
    ):
        assert _create(client, admin_headers, org="acme").status_code == 201
        token = create_access_token(
            data={"sub": "alice", "org_id": "globex"}, settings=api_settings
        )

        assert _session_org(client, {"Authorization": f"Bearer {token}"}) == "acme"

    def test_an_update_moves_the_account_to_another_org(
        self, client: TestClient, admin_headers: dict[str, str], orgs: None
    ):
        assert _create(client, admin_headers, org="acme").status_code == 201

        resp = client.put(f"{ACCOUNTS}/alice", json={"org": "globex"}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["org"] == "globex"
        assert _session_org(client, _sign_in(client, "alice")) == "globex"

    def test_an_update_to_an_unknown_org_is_refused(
        self, app: FastAPI, client: TestClient, admin_headers: dict[str, str], orgs: None
    ):
        assert _create(client, admin_headers, org="acme").status_code == 201

        resp = client.put(f"{ACCOUNTS}/alice", json={"org": "initech"}, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert app.state.account_store.get("alice").org == "acme"


@pytest.fixture
def reader(app: FastAPI, client: TestClient) -> RecordingClient:
    """A tenant-isolated view over a client that records what it was asked to bind."""
    recording = RecordingClient()
    app.state.view_executor = ViewExecutor(
        restricted_client=recording, catalog=OneViewCatalog(view_definition()), database="dfe"
    )
    return recording


def _tenant_viewer(app: FastAPI, client: TestClient, admin_headers, *, org: str, grants: list[str]):
    """alice, bound to *org*, holding org_viewer system-wide over the orgs in *grants*."""
    assert _create(client, admin_headers, org=org).status_code == 201
    app.state.group_store.create("tenant-viewers", roles=["org_viewer"], members=["alice"])
    app.state.group_store.update("tenant-viewers", org_ids=grants)
    app.state.account_store.update("alice", groups=["tenant-viewers"])
    return _sign_in(client, "alice")


class TestATenantIsolatedViewReadsOnlyTheSessionsOrg:
    def test_a_session_bound_to_no_org_is_refused_before_any_query(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        headers = _tenant_viewer(app, client, admin_headers, org="", grants=["acme"])

        resp = client.post(EXECUTE, json={}, headers=headers)

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "no_org"
        assert reader.sql == ""

    def test_a_session_bound_to_an_org_its_groups_do_not_grant_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        headers = _tenant_viewer(app, client, admin_headers, org="acme", grants=["globex"])

        resp = client.post(EXECUTE, json={}, headers=headers)

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "org_not_granted"
        assert reader.sql == ""

    def test_a_session_reads_the_org_it_is_bound_to(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        headers = _tenant_viewer(app, client, admin_headers, org="acme", grants=["acme"])

        resp = client.post(EXECUTE, json={}, headers=headers)

        assert resp.status_code == 200, resp.text
        assert reader.parameters["org_id"] == "acme"

    def test_a_platform_reader_reads_the_org_it_is_bound_to(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        assert _create(client, admin_headers, org="globex").status_code == 201
        app.state.group_store.add_member("dfe-viewers", "alice")
        app.state.account_store.update("alice", groups=["dfe-viewers"])

        resp = client.post(EXECUTE, json={}, headers=_sign_in(client, "alice"))

        assert resp.status_code == 200, resp.text
        assert reader.parameters["org_id"] == "globex"
