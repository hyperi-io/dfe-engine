#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_scoped_role_read_only_api.py
#  Purpose:      The roles API refuses a scoped role granting anything beyond reads
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Creating or editing a role through the API cannot make a scoped role that writes."""

import pytest

ROLES = "/api/v1/auth/roles"
WRITES = ["org:write", "group:write", "repository:write"]


def _refused(resp, role: str, write: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "scoped_role_not_read_only"
    assert f"'{role}'" in body["message"]
    assert body["message"].endswith(f"not {write}")


@pytest.mark.parametrize("write", WRITES)
def test_creating_a_scoped_role_granting_a_write_is_refused(app, client, admin_headers, write):
    body = {"name": "tenant_admin", "permissions": ["org:read", write], "scoped": True}

    _refused(client.post(ROLES, json=body, headers=admin_headers), "tenant_admin", write)
    assert app.state.role_store.get("tenant_admin") is None


@pytest.mark.parametrize("write", WRITES)
def test_scoping_a_role_that_grants_a_write_is_refused(app, client, admin_headers, write):
    body = {"name": "admins_of", "permissions": [write], "scoped": False}
    assert client.post(ROLES, json=body, headers=admin_headers).status_code == 201

    resp = client.put(f"{ROLES}/admins_of", json={"scoped": True}, headers=admin_headers)

    _refused(resp, "admins_of", write)
    assert app.state.role_store.get("admins_of").scoped is False


@pytest.mark.parametrize("write", WRITES)
def test_adding_a_write_to_a_scoped_role_is_refused(app, client, admin_headers, write):
    body = {"name": "tenant_reader", "permissions": ["org:read"], "scoped": True}
    assert client.post(ROLES, json=body, headers=admin_headers).status_code == 201

    resp = client.put(
        f"{ROLES}/tenant_reader", json={"permissions": ["org:read", write]}, headers=admin_headers
    )

    _refused(resp, "tenant_reader", write)
    assert app.state.role_store.get("tenant_reader").permissions == ["org:read"]


def test_a_scoped_role_of_reads_and_query_execute_is_created(client, admin_headers):
    body = {"name": "tenant_view", "permissions": ["query:execute", "usage:read"], "scoped": True}

    assert client.post(ROLES, json=body, headers=admin_headers).status_code == 201
