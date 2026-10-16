#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_view_custom_role.py
#  Purpose:      A custom role that passes the view route also passes the view executor
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The view executor reads the deployment's live role definitions, as the route does.

It read only the built-in roles, so a custom role holding ``query:execute`` passed
the route's check and was refused by the executor behind it.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dfe_engine import settings as settings_module
from tests.unit.test_query.view_fakes import RecordingClient

EXECUTE = "/api/v1/queries/views/analytics/events/execute"
PASSWORD = "pw-runner-123456"


@pytest.fixture
def executor_auth_on(api_settings, monkeypatch) -> None:
    """The executor checks auth against the process settings, so give it the app's."""
    monkeypatch.setattr(settings_module, "get_settings", lambda: api_settings)


@pytest.mark.usefixtures("executor_auth_on")
def test_a_custom_role_holding_query_execute_runs_a_view(
    app: FastAPI, client: TestClient, admin_headers: dict[str, str], reader: RecordingClient
):
    assert (
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers).status_code == 201
    )
    role = client.post(
        "/api/v1/auth/roles",
        json={"name": "view_runner", "permissions": ["query:execute"], "scoped": True},
        headers=admin_headers,
    )
    assert role.status_code == 201, role.text
    app.state.group_store.create("runners", roles=["view_runner"], members=["runner"])
    app.state.group_store.update("runners", org_ids=["acme"])
    created = client.post(
        "/api/v1/auth/accounts",
        json={"username": "runner", "password": PASSWORD, "groups": ["runners"], "org": "acme"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/v1/auth/login", json={"username": "runner", "password": PASSWORD})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.post(EXECUTE, json={}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert reader.parameters["org_id"] == "acme"
