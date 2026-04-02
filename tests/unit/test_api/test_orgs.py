#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_orgs.py
#  Purpose:      Tests for org CRUD REST endpoints
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
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
        assert resp.json() == []

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
        names = {o["name"] for o in resp.json()}
        assert names == {"acme", "beta"}

    def test_list_viewer_can_read(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=viewer_headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


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


# ---------------------------------------------------------------------------
# dedicated_database / OrgLifecycleManager wiring
# ---------------------------------------------------------------------------


class TestDedicatedDatabase:
    def test_create_org_with_dedicated_database(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "dd-test", "dedicated_database": True},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["dedicated_database"] is True

    def test_create_org_dedicated_database_defaults_false(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "dd-test"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["dedicated_database"] is False

    def test_dedicated_db_disable_requires_confirm(self, client, admin_headers):
        # Create org (dedicated_database defaults to False)
        client.post(
            "/api/v1/orgs",
            json={"name": "confirm-test", "org_ids": ["ct"]},
            headers=admin_headers,
        )
        # Update with dedicated_database=False (same as current state) — no toggle needed, should 200
        resp = client.put(
            "/api/v1/orgs/confirm-test",
            json={"dedicated_database": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200

    def test_dedicated_db_disable_without_confirm_returns_400(self, client, admin_headers):
        # Create org with dedicated_database enabled
        client.post(
            "/api/v1/orgs",
            json={"name": "disable-test", "dedicated_database": True},
            headers=admin_headers,
        )
        # Try to disable without confirm_merge — should get 400
        resp = client.put(
            "/api/v1/orgs/disable-test",
            json={"dedicated_database": False},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "confirmation_required"

    def test_dedicated_db_disable_with_confirm_succeeds(self, client, admin_headers):
        # Create org with dedicated_database enabled
        client.post(
            "/api/v1/orgs",
            json={"name": "disable-confirm-test", "dedicated_database": True},
            headers=admin_headers,
        )
        # Disable with confirm_merge=True — should succeed
        resp = client.put(
            "/api/v1/orgs/disable-confirm-test",
            json={"dedicated_database": False, "confirm_merge": True},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["dedicated_database"] is False

    def test_response_includes_dedicated_database_field(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "field-test"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/field-test", headers=admin_headers)
        assert resp.status_code == 200
        assert "dedicated_database" in resp.json()
