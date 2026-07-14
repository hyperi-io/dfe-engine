"""Tests for sources router — CRUD, pagination, search, sort, bulk."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dfe_engine.api.deps import _registries
from dfe_engine.api.v1.sources import _raise_save_validation_http
from dfe_engine.source.registry import SourceValidationError


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
        names = [item["name"] for item in data["items"]]
        assert "test_source" in names
        assert "objects" in data
        assert data["items"][0]["versions"] == ["1.0.0"]
        assert data["items"][0]["current"] == "1.0.0"
        assert data["items"][0]["deployed_version"] is None

    def test_list_pagination(self, client: TestClient, admin_headers: dict):
        # Create 5 sources
        for i in range(5):
            client.post(
                "/api/v1/sources",
                json={
                    "source": f"src_{i}",
                    "enabled": True,
                    "match": {"field": "tags.collector.type", "value": f"src_{i}"},
                },
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
            json={
                "source": "windows_audit",
                "display_name": "Windows Audit Logs",
                "match": {"field": "tags.collector.type", "value": "windows_audit"},
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={
                "source": "linux_syslog",
                "display_name": "Linux Syslog",
                "match": {"field": "tags.collector.type", "value": "linux_syslog"},
            },
            headers=admin_headers,
        )

        resp = client.get("/api/v1/sources?search=windows", headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "windows_audit"

    def test_list_sort(self, client: TestClient, admin_headers: dict):
        for name in ["charlie", "alpha", "bravo"]:
            client.post(
                "/api/v1/sources",
                json={
                    "source": name,
                    "match": {"field": "tags.collector.type", "value": name},
                },
                headers=admin_headers,
            )

        resp = client.get("/api/v1/sources?sort_by=source&sort_order=asc", headers=admin_headers)
        data = resp.json()
        names = [item["name"] for item in data["items"]]
        assert names == sorted(names)

    def test_list_object_tree_places_sources_at_root(self, client: TestClient, admin_headers: dict):
        client.post(
            "/api/v1/sources",
            json={
                "source": "aws_cloudtrail",
                "display_name": "AWS CloudTrail",
                "match": {"field": "tags.collector.type", "value": "aws_cloudtrail"},
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={
                "source": "dfe_alerts",
                "display_name": "DFE Alerts",
                "match": {"field": "tags.collector.type", "value": "dfe_alerts"},
            },
            headers=admin_headers,
        )
        resp = client.get("/api/v1/sources", headers=admin_headers)
        data = resp.json()
        root_names = {item["name"] for item in data["objects"]["items"]}
        assert "aws_cloudtrail" in root_names
        assert "dfe_alerts" in root_names
        assert data["objects"]["children"] == {}

        resp = client.get("/api/v1/sources")
        assert resp.status_code == 401

    def test_list_viewer_can_read(self, client: TestClient, viewer_headers: dict):
        resp = client.get("/api/v1/sources", headers=viewer_headers)
        assert resp.status_code == 200

    def test_list_enabled_false_returns_only_disabled(
        self, client: TestClient, admin_headers: dict
    ):
        client.post(
            "/api/v1/sources",
            json={
                "source": "enabled_only_src",
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": "enabled_only_src"},
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={
                "source": "disabled_only_src",
                "enabled": False,
                "match": {"field": "tags.collector.type", "value": "disabled_only_src"},
            },
            headers=admin_headers,
        )
        resp = client.get("/api/v1/sources?enabled=false", headers=admin_headers)
        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()["items"]]
        assert "disabled_only_src" in names
        assert "enabled_only_src" not in names


class TestSaveValidationHttpMapping:
    def test_generic_validation_error_is_422(self):
        with pytest.raises(HTTPException) as exc_info:
            _raise_save_validation_http(SourceValidationError("invalid source definition"))
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "validation_error"


class TestCreateSource:
    """POST /api/v1/sources"""

    def test_create_success(self, client: TestClient, admin_headers: dict, sample_source: dict):
        resp = client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["source"] == "test_source"
        assert data["message"] == "created"
        assert data["current"] == "1.0.0"
        assert data["deployed_version"] is None
        assert data["versions"] == ["1.0.0"]

        get_resp = client.get("/api/v1/sources/test_source", headers=admin_headers)
        body = get_resp.json()
        assert body["current"] == "1.0.0"
        assert body["deployed_version"] is None
        assert "1.0.0" in body["versions"]

    def test_create_rejects_version_tree_in_body(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        resp = client.post(
            "/api/v1/sources",
            json={
                **sample_source,
                "versions": {"1.0.0": {"date_time": "2026-01-01", "schema": {}}},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422

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
        ver = data["versions"]["1.0.0"]
        assert ver["source_build"] is None
        assert ver["source_deployment"] is None

    def test_get_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources/nonexistent", headers=admin_headers)
        assert resp.status_code == 404


class TestGetSourceVersion:
    """GET /api/v1/sources/{name}/versions/{version}"""

    def test_get_version_after_create(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            "/api/v1/sources/test_source/versions/1.0.0",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "test_source"
        assert body["selected"] == "1.0.0"
        assert body["current"] == "1.0.0"
        assert body["versions"] == ["1.0.0"]
        assert body["previous_deployed_versions"] == []
        assert body["version"]["schema"]["engine"] == "MergeTree"
        assert body["version"]["source_build"] is None
        assert body["version"]["source_deployment"] is None

    def test_get_version_after_update_preserves_history(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        client.put(
            "/api/v1/sources/test_source",
            json={**sample_source, "description": "v2"},
            headers=admin_headers,
        )
        v1 = client.get(
            "/api/v1/sources/test_source/versions/1.0.0",
            headers=admin_headers,
        )
        assert v1.status_code == 200
        assert v1.json()["selected"] == "1.0.0"
        assert v1.json()["current"] == "1.0.0"
        assert v1.json()["description"] == "v2"

    def test_get_version_after_bump_worthy_update_appends_version(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        registry = _registries["source"]
        registry.set_deployed_version("test_source", "1.0.0")

        client.put(
            "/api/v1/sources/test_source",
            json={
                **sample_source,
                "schema": {
                    **sample_source["schema_config"],
                    "meta_schema_version": "2.0.0",
                },
            },
            headers=admin_headers,
        )
        v2 = client.get(
            "/api/v1/sources/test_source/versions/2.0.0",
            headers=admin_headers,
        )
        assert v2.status_code == 200
        assert v2.json()["current"] == "2.0.0"
        assert v2.json()["version"]["schema"]["meta_schema_version"] == "2.0.0"

    def test_get_version_not_found(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            "/api/v1/sources/test_source/versions/9.9.9",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_get_version_source_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.get(
            "/api/v1/sources/missing/versions/1.0.0",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_get_version_path_requires_version_segment(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get("/api/v1/sources/test_source/versions", headers=admin_headers)
        assert resp.status_code == 404

    def test_get_version_includes_top_level_metadata(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        body = {
            **sample_source,
            "match": {"field": "ingest_type", "value": "test_ingest"},
            "transform": {"engine": "vector", "config_file": "/etc/vector/test.toml"},
        }
        client.post("/api/v1/sources", json=body, headers=admin_headers)
        resp = client.get(
            "/api/v1/sources/test_source/versions/1.0.0",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == sample_source["display_name"]
        assert data["deployed_version"] is None
        assert data["version"]["match"]["field"] == "ingest_type"
        assert data["version"]["transform"]["engine"] == "vector"


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
        updated = resp.json()
        assert updated["message"] == "updated"
        assert updated["current"] == "1.0.0"
        assert updated["deployed_version"] is None
        assert updated["versions"] == ["1.0.0"]

        # Verify the update persisted
        get_resp = client.get("/api/v1/sources/test_source", headers=admin_headers)
        assert get_resp.json()["description"] == "Updated description"
        body = get_resp.json()
        assert "1.0.0" in body["versions"]
        assert "2.0.0" not in body["versions"]
        assert body["current"] == "1.0.0"
        assert body["versions"]["1.0.0"]["schema"]["engine"] == "MergeTree"

    def test_update_rejects_versions_payload(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.put(
            "/api/v1/sources/test_source",
            json={
                **sample_source,
                "versions": {"1.0.0": {"date_time": "2026-01-01", "schema": {}}},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_update_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.put(
            "/api/v1/sources/nonexistent",
            json={
                "source": "nonexistent",
                "match": {"field": "tags.collector.type", "value": "nonexistent"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_not_found_when_registry_raises_after_exists_check(
        self,
        monkeypatch,
        client: TestClient,
        admin_headers: dict,
        sample_source: dict,
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        registry = _registries["source"]
        from dfe_engine.source.registry import SourceNotFoundError

        def missing(*args, **kwargs):
            raise SourceNotFoundError("test_source")

        monkeypatch.setattr(registry, "update_source_from_write", missing)
        resp = client.put(
            "/api/v1/sources/test_source",
            json={**sample_source, "description": "gone"},
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
            "views": [],
        }
        second = {
            **sample_source,
            "source": "source_beta",
            "match": {"field": "ingest_type", "value": "other_value"},
            "views": [],
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


class TestPatchSourceEnabled:
    """PATCH /api/v1/sources/{name}"""

    def test_disable_and_enable(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)

        disable = client.patch(
            "/api/v1/sources/test_source",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert disable.status_code == 200
        assert disable.json()["message"] == "disabled"
        assert (
            client.get("/api/v1/sources/test_source", headers=admin_headers).json()["enabled"]
            is False
        )

        enable = client.patch(
            "/api/v1/sources/test_source",
            json={"enabled": True},
            headers=admin_headers,
        )
        assert enable.status_code == 200
        assert enable.json()["message"] == "active"
        assert (
            client.get("/api/v1/sources/test_source", headers=admin_headers).json()["enabled"]
            is True
        )

    def test_patch_state_dormant(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test_source",
            json={"state": "dormant"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "dormant"
        detail = client.get("/api/v1/sources/test_source", headers=admin_headers).json()
        assert detail["state"] == "dormant"
        assert detail["enabled"] is False  # compat accessor

    def test_patch_requires_state_or_enabled(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test_source",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_idempotent_when_already_enabled(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test_source",
            json={"enabled": True},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "active"

    def test_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.patch(
            "/api/v1/sources/missing_src",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_rejects_extra_fields(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test_source",
            json={"enabled": False, "description": "nope"},
            headers=admin_headers,
        )
        assert resp.status_code == 422


class TestBulkAction:
    """POST /api/v1/sources/bulk"""

    def test_bulk_delete(self, client: TestClient, admin_headers: dict):
        for name in ["bulk_a", "bulk_b", "bulk_c"]:
            client.post(
                "/api/v1/sources",
                json={
                    "source": name,
                    "match": {"field": "tags.collector.type", "value": name},
                },
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

    def test_bulk_disable_and_enable(self, client: TestClient, admin_headers: dict):
        client.post(
            "/api/v1/sources",
            json={
                "source": "bulk_toggle",
                "match": {"field": "tags.collector.type", "value": "bulk_toggle"},
            },
            headers=admin_headers,
        )
        disable = client.post(
            "/api/v1/sources/bulk",
            json={"action": "disable", "sources": ["bulk_toggle"]},
            headers=admin_headers,
        )
        assert disable.status_code == 200
        assert disable.json()["succeeded"] == ["bulk_toggle"]
        assert (
            client.get("/api/v1/sources/bulk_toggle", headers=admin_headers).json()["enabled"]
            is False
        )

        enable = client.post(
            "/api/v1/sources/bulk",
            json={"action": "enable", "sources": ["bulk_toggle"]},
            headers=admin_headers,
        )
        assert enable.status_code == 200
        assert enable.json()["succeeded"] == ["bulk_toggle"]
        assert (
            client.get("/api/v1/sources/bulk_toggle", headers=admin_headers).json()["enabled"]
            is True
        )

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
