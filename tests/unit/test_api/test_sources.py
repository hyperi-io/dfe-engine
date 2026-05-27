"""Tests for sources router — CRUD, pagination, search, sort, bulk."""

from fastapi.testclient import TestClient


class TestListSources:
    """GET /api/v1/sources"""

    def test_list_empty(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["total_pages"] == 1

    def test_list_after_create(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get("/api/v1/sources", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        names = [item["source"] for item in data["items"]]
        assert "test_source" in names

    def test_list_pagination(self, client: TestClient, admin_headers: dict):
        # Create 5 sources
        for i in range(5):
            client.post(
                "/api/v1/sources",
                json={"source": f"src_{i}", "enabled": True},
                headers=admin_headers,
            )

        # Page 1, 2 per page
        resp = client.get("/api/v1/sources?page=1&per_page=2", headers=admin_headers)
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 5
        assert data["total_pages"] == 3
        assert data["next_page"] == 2
        assert data["prev_page"] is None

        # Page 3 (last)
        resp = client.get("/api/v1/sources?page=3&per_page=2", headers=admin_headers)
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["next_page"] is None
        assert data["prev_page"] == 2

    def test_list_invalid_per_page_returns_422(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources?page=1&per_page=0", headers=admin_headers)
        assert resp.status_code == 422

    def test_list_search(self, client: TestClient, admin_headers: dict):
        client.post(
            "/api/v1/sources",
            json={"source": "windows_audit", "display_name": "Windows Audit Logs"},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={"source": "linux_syslog", "display_name": "Linux Syslog"},
            headers=admin_headers,
        )

        resp = client.get("/api/v1/sources?search=windows", headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["source"] == "windows_audit"

    def test_list_sort(self, client: TestClient, admin_headers: dict):
        for name in ["charlie", "alpha", "bravo"]:
            client.post(
                "/api/v1/sources",
                json={"source": name},
                headers=admin_headers,
            )

        resp = client.get("/api/v1/sources?sort_by=source&sort_order=asc", headers=admin_headers)
        data = resp.json()
        names = [item["source"] for item in data["items"]]
        assert names == sorted(names)

    def test_list_requires_auth(self, client: TestClient):
        resp = client.get("/api/v1/sources")
        assert resp.status_code == 401

    def test_list_viewer_can_read(self, client: TestClient, viewer_headers: dict):
        resp = client.get("/api/v1/sources", headers=viewer_headers)
        assert resp.status_code == 200


class TestCreateSource:
    """POST /api/v1/sources"""

    def test_create_success(self, client: TestClient, admin_headers: dict, sample_source: dict):
        resp = client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["source"] == "test_source"
        assert data["message"] == "created"

    def test_create_duplicate(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        assert resp.status_code == 409

    def test_create_missing_source_field(self, client: TestClient, admin_headers: dict):
        resp = client.post(
            "/api/v1/sources",
            json={"display_name": "No Source Name"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_viewer_forbidden(
        self, client: TestClient, viewer_headers: dict, sample_source: dict
    ):
        resp = client.post("/api/v1/sources", json=sample_source, headers=viewer_headers)
        assert resp.status_code == 403


class TestGetSource:
    """GET /api/v1/sources/{name}"""

    def test_get_existing(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get("/api/v1/sources/test_source", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "test_source"
        assert data["display_name"] == "Test Source"

    def test_get_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources/nonexistent", headers=admin_headers)
        assert resp.status_code == 404


class TestUpdateSource:
    """PUT /api/v1/sources/{name}"""

    def test_update_success(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.put(
            "/api/v1/sources/test_source",
            json={**sample_source, "description": "Updated description"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "updated"

        # Verify the update persisted
        get_resp = client.get("/api/v1/sources/test_source", headers=admin_headers)
        assert get_resp.json()["description"] == "Updated description"

    def test_update_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.put(
            "/api/v1/sources/nonexistent",
            json={"source": "nonexistent"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_match_conflict_returns_409(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        shared = {"field": "ingest_type", "value": "shared_value"}
        first = {
            **sample_source,
            "source": "source_alpha",
            "match": shared,
            "mapping_standards": [],
        }
        second = {
            **sample_source,
            "source": "source_beta",
            "match": {"field": "ingest_type", "value": "other_value"},
            "mapping_standards": [],
        }
        assert client.post("/api/v1/sources", json=first, headers=admin_headers).status_code == 201
        assert client.post("/api/v1/sources", json=second, headers=admin_headers).status_code == 201

        create_dup = client.post(
            "/api/v1/sources",
            json={**second, "source": "source_gamma", "match": shared},
            headers=admin_headers,
        )
        assert create_dup.status_code == 409
        dup_body = create_dup.json()
        assert dup_body["code"] == "match_conflict"
        assert dup_body["context"]["conflicting_source"] == "source_alpha"
        assert dup_body["context"]["source"] == "source_gamma"
        assert dup_body["context"]["field"] == "ingest_type"
        assert dup_body["context"]["value"] == "shared_value"
        assert "source_alpha" in dup_body["message"]

        conflict_put = client.put(
            "/api/v1/sources/source_beta",
            json={**second, "match": shared},
            headers=admin_headers,
        )
        assert conflict_put.status_code == 409
        assert conflict_put.json()["code"] == "match_conflict"


class TestDeleteSource:
    """DELETE /api/v1/sources/{name}"""

    def test_delete_success(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.delete("/api/v1/sources/test_source", headers=admin_headers)
        assert resp.status_code == 204

        # Verify it's gone
        get_resp = client.get("/api/v1/sources/test_source", headers=admin_headers)
        assert get_resp.status_code == 404

    def test_delete_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.delete("/api/v1/sources/nonexistent", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_viewer_forbidden(
        self, client: TestClient, viewer_headers: dict, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.delete("/api/v1/sources/test_source", headers=viewer_headers)
        assert resp.status_code == 403


class TestBulkAction:
    """POST /api/v1/sources/bulk"""

    def test_bulk_delete(self, client: TestClient, admin_headers: dict):
        for name in ["bulk_a", "bulk_b", "bulk_c"]:
            client.post(
                "/api/v1/sources",
                json={"source": name},
                headers=admin_headers,
            )

        resp = client.post(
            "/api/v1/sources/bulk",
            json={"action": "delete", "sources": ["bulk_a", "bulk_c"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["succeeded"]) == {"bulk_a", "bulk_c"}
        assert data["failed"] == []

    def test_bulk_invalid_action(self, client: TestClient, admin_headers: dict):
        resp = client.post(
            "/api/v1/sources/bulk",
            json={"action": "nope", "sources": ["x"]},
            headers=admin_headers,
        )
        assert resp.status_code == 422


class TestSeedSources:
    """POST /api/v1/sources/seed"""

    def test_seed(self, client: TestClient, admin_headers: dict):
        resp = client.post("/api/v1/sources/seed", headers=admin_headers)
        assert resp.status_code == 200
        assert "seeded" in resp.json()
