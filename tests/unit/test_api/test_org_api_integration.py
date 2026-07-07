#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_api_integration.py
#  Purpose:      Integration tests for org API endpoints with lifecycle manager wired in
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Integration tests for org API endpoints.

Uses the shared ``client`` / ``admin_headers`` fixtures from conftest.py
so registries are backed by tmp_path and a real app instance runs.
"""

from __future__ import annotations

# ── Fixtures are provided by tests/unit/test_api/conftest.py via pytest conftest
# inheritance.  The ``client`` fixture uses tmp_path isolation, so each test
# class method gets a fresh app instance.


class TestOrgApiWithLifecycle:
    """Test org API endpoints with OrgLifecycleManager wired in."""

    def test_create_returns_enabled_field(self, client, admin_headers):
        """POST /api/v1/orgs response includes the enabled field (default True)."""
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "api-test", "org_ids": ["at"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["enabled"] is True

    def test_create_org_response_shape(self, client, admin_headers):
        """POST response includes all required OrgResponse fields."""
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "shape-test", "org_ids": ["st"], "display_name": "Shape Test"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        for field in (
            "name",
            "display_name",
            "org_ids",
            "enabled",
            "created_at",
            "updated_at",
        ):
            assert field in data, f"Missing field: {field}"

    def test_list_orgs_returns_created(self, client, admin_headers):
        """GET /api/v1/orgs returns the created orgs as a plain array."""
        client.post(
            "/api/v1/orgs",
            json={"name": "list-test", "org_ids": ["lt"]},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=admin_headers)
        assert resp.status_code == 200
        orgs = resp.json()["items"]
        # The list endpoint returns a paginated envelope; items is the array
        assert isinstance(orgs, list)
        names = [o["name"] for o in orgs]
        assert "list-test" in names

    def test_get_org_by_name(self, client, admin_headers):
        """GET /api/v1/orgs/{name} returns correct org."""
        client.post(
            "/api/v1/orgs",
            json={"name": "get-me", "org_ids": ["gm"], "display_name": "Get Me"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/get-me", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "get-me"
        assert resp.json()["display_name"] == "Get Me"

    def test_duplicate_org_returns_409(self, client, admin_headers):
        """Creating the same org name twice returns 409 Conflict."""
        client.post(
            "/api/v1/orgs",
            json={"name": "dup-test", "org_ids": ["d"]},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "dup-test", "org_ids": ["d"]},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_get_nonexistent_org_returns_404(self, client, admin_headers):
        """GET /api/v1/orgs/{name} for missing org returns 404 with error shape."""
        resp = client.get("/api/v1/orgs/nonexistent-xyz", headers=admin_headers)
        assert resp.status_code == 404
        data = resp.json()
        assert "code" in data
        assert "message" in data

    def test_update_org_display_name(self, client, admin_headers):
        """PUT /api/v1/orgs/{name} updates display_name."""
        client.post(
            "/api/v1/orgs",
            json={"name": "upd-me", "org_ids": ["um"], "display_name": "Before"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/upd-me",
            json={"display_name": "After"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "After"

    def test_delete_org_returns_204(self, client, admin_headers):
        """DELETE /api/v1/orgs/{name} returns 204 and org is gone."""
        client.post(
            "/api/v1/orgs",
            json={"name": "del-me", "org_ids": ["dm"]},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/orgs/del-me", headers=admin_headers)
        assert resp.status_code in (200, 204)

        resp = client.get("/api/v1/orgs/del-me", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_nonexistent_org_returns_404(self, client, admin_headers):
        """DELETE on nonexistent org returns 404."""
        resp = client.delete("/api/v1/orgs/ghost-xyz", headers=admin_headers)
        assert resp.status_code == 404

    def test_update_nonexistent_org_returns_404(self, client, admin_headers):
        """PUT on nonexistent org returns 404."""
        resp = client.put(
            "/api/v1/orgs/no-such-org",
            json={"display_name": "X"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_viewer_cannot_create_org(self, client, viewer_headers):
        """Read-only users cannot create orgs (403)."""
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "viewer-attempt", "org_ids": ["va"]},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_viewer_cannot_delete_org(self, client, admin_headers, viewer_headers):
        """Read-only users cannot delete orgs (403)."""
        client.post(
            "/api/v1/orgs",
            json={"name": "viewer-del-test", "org_ids": ["vdt"]},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/orgs/viewer-del-test", headers=viewer_headers)
        assert resp.status_code == 403

    def test_org_ids_persisted_correctly(self, client, admin_headers):
        """org_ids list is stored and returned accurately."""
        org_ids = ["tenant-a", "tenant-b", "tenant-c"]
        client.post(
            "/api/v1/orgs",
            json={"name": "multi-tenant", "org_ids": org_ids},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/multi-tenant", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["org_ids"] == org_ids
