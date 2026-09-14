#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_orgs.py
#  Purpose:      Tests for org CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for /api/v1/orgs endpoints.

Uses the shared conftest fixtures (client, admin_headers, viewer_headers).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# POST /api/v1/orgs
# ---------------------------------------------------------------------------


class TestCreateOrg:
    def test_create_org_returns_201(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["acme", "acme-sub"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "acme"
        assert data["org_ids"] == ["acme", "acme-sub"]
        assert data["enabled"] is True

    def test_create_org_with_display_name(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme", "display_name": "Acme Corp"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["display_name"] == "Acme Corp"

    def test_create_org_duplicate_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_org_path_traversal_name_rejected(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            headers=admin_headers,
            json={"name": "../../escape"},
        )
        assert resp.status_code == 422
        listed = client.get("/api/v1/orgs", headers=admin_headers).json()
        assert listed["items"] == []

    def test_create_org_viewer_forbidden(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_org_no_auth_returns_401(self, client):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
        )
        assert resp.status_code == 401

    def test_create_org_sets_timestamps(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        data = resp.json()
        assert data["created_at"] != ""
        assert data["updated_at"] != ""


# ---------------------------------------------------------------------------
# GET /api/v1/orgs
# ---------------------------------------------------------------------------


class TestListOrgs:
    def test_list_empty_returns_empty(self, client, admin_headers):
        resp = client.get("/api/v1/orgs", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_list_returns_created_orgs(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/orgs",
            json={"name": "beta"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=admin_headers)
        assert resp.status_code == 200
        names = {o["name"] for o in resp.json()["items"]}
        assert names == {"acme", "beta"}

    def test_list_viewer_can_read(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=viewer_headers)
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1


# ---------------------------------------------------------------------------
# GET /api/v1/orgs/{name}
# ---------------------------------------------------------------------------


class TestGetOrg:
    def test_get_existing_org(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "display_name": "Acme Corp"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "acme"
        assert resp.json()["display_name"] == "Acme Corp"

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/orgs/nobody", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_viewer_can_read(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/acme", headers=viewer_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/v1/orgs/{name}
# ---------------------------------------------------------------------------


class TestUpdateOrg:
    def test_update_display_name(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"display_name": "Acme Updated"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Acme Updated"

    def test_update_org_ids(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["acme"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"org_ids": ["acme", "acme-new"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["org_ids"] == ["acme", "acme-new"]

    def test_update_disable_org(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_update_nonexistent_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/orgs/nobody",
            json={"display_name": "?"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_viewer_forbidden(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"display_name": "Hacked"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /api/v1/orgs/{name}
# ---------------------------------------------------------------------------


class TestDeleteOrg:
    def test_delete_returns_204(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 204

    def test_delete_removes_org(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        client.delete("/api/v1/orgs/acme", headers=admin_headers)
        resp = client.get("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/orgs/nobody", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_viewer_forbidden(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/orgs/acme", headers=viewer_headers)
        assert resp.status_code == 403
