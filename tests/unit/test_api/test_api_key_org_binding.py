#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_api_key_org_binding.py
#  Purpose:      An API key's session carries the org the key is bound to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An API key binds its sessions to one registered org, the way an account does.

A key's session carried no org and nothing could give it one, so a view held to
one org refused every machine credential.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.test_query.view_fakes import RecordingClient

KEYS = "/api/v1/auth/api-keys"
EXECUTE = "/api/v1/queries/views/analytics/events/execute"


@pytest.fixture
def orgs(client: TestClient, admin_headers: dict[str, str]) -> None:
    for org in ("acme", "globex"):
        resp = client.post("/api/v1/orgs", json={"name": org}, headers=admin_headers)
        assert resp.status_code == 201, resp.text


def _key(client: TestClient, admin_headers: dict[str, str], app: FastAPI, **fields):
    """Mint a key in a group holding org_viewer over acme."""
    if app.state.group_store.get("tenant-viewers") is None:
        app.state.group_store.create("tenant-viewers", roles=["org_viewer"])
        app.state.group_store.update("tenant-viewers", org_ids=["acme"])
    body = {"name": "pipeline", "groups": ["tenant-viewers"], **fields}
    return client.post(KEYS, json=body, headers=admin_headers)


class TestAnApiKeyIsBoundToAnOrg:
    def test_a_key_bound_to_an_org_reads_that_org(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        created = _key(client, admin_headers, app, org="acme")
        assert created.status_code == 201, created.text
        assert created.json()["org"] == "acme"

        resp = client.post(EXECUTE, json={}, headers={"X-API-Key": created.json()["full_key"]})

        assert resp.status_code == 200, resp.text
        assert reader.parameters["org_id"] == "acme"

    def test_a_key_bound_to_no_org_is_refused_before_any_query(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        created = _key(client, admin_headers, app)
        assert created.status_code == 201, created.text

        resp = client.post(EXECUTE, json={}, headers={"X-API-Key": created.json()["full_key"]})

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "no_org"
        assert reader.sql == ""

    def test_a_key_bound_to_an_org_its_groups_do_not_grant_is_refused(
        self,
        app: FastAPI,
        client: TestClient,
        admin_headers: dict[str, str],
        orgs: None,
        reader: RecordingClient,
    ):
        created = _key(client, admin_headers, app, org="globex")
        assert created.status_code == 201, created.text

        resp = client.post(EXECUTE, json={}, headers={"X-API-Key": created.json()["full_key"]})

        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "org_not_granted"
        assert reader.sql == ""

    def test_an_org_the_registry_does_not_hold_is_refused_and_no_key_is_minted(
        self, app: FastAPI, client: TestClient, admin_headers: dict[str, str], orgs: None
    ):
        resp = _key(client, admin_headers, app, org="initech")

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "unknown_org"
        assert app.state.api_key_store.get("pipeline") is None

    def test_the_listing_shows_each_keys_org(
        self, app: FastAPI, client: TestClient, admin_headers: dict[str, str], orgs: None
    ):
        assert _key(client, admin_headers, app, org="acme").status_code == 201

        listed = client.get(KEYS, headers=admin_headers).json()["items"]

        assert [(row["name"], row["org"]) for row in listed] == [("pipeline", "acme")]
