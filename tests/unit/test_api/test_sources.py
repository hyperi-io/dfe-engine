"""Tests for sources router — CRUD, pagination, search, sort, bulk."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dfe_engine.api.deps import _registries
from dfe_engine.api.v1.sources import _raise_save_validation_http
from dfe_engine.kafka.topics import TopicRemoveResult
from dfe_engine.source.registry import SourceValidationError


def _removed(seen: list[str], names: list[str]) -> TopicRemoveResult:
    """Record what a delete asked the broker to remove, and report it as done."""
    seen.extend(names)
    return TopicRemoveResult(removed=list(names))


class TestListSources:
    """GET /api/v1/sources"""

    def test_list_before_any_source_is_created(self, client: TestClient, admin_headers: dict):
        # Never empty: the engine seeds the landing table's own source on startup.
        resp = client.get("/api/v1/sources", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert [item["name"] for item in data["items"]] == ["main"]
        assert data["items"][0]["resource_type"] == "core"
        assert data["items"][0]["origin"] is None
        assert data["items"][0]["current_table_topic_type"] == "main"
        assert data["total"] == 1
        assert data["page"] == 1
        assert data["total_pages"] == 1

    def test_list_after_create(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get("/api/v1/sources", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        by_name = {item["name"]: item for item in data["items"]}
        assert "test-source" in by_name
        assert "objects" in data
        created = by_name["test-source"]
        assert created["versions"] == ["1.0.0"]
        assert created["current"] == "1.0.0"
        assert created["deployed_version"] is None
        # sample_source pins no meta_schema, so records share the landing table.
        assert created["current_table_topic_type"] == "main"
        assert by_name["main"]["current_table_topic_type"] == "main"

    def test_list_reports_own_when_a_meta_schema_is_pinned(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post(
            "/api/v1/sources",
            json={
                **sample_source,
                "source": "aws-own",
                "schema_config": {
                    "meta_schema": "meta/aws_cloudtrail",
                    "meta_schema_version": "1.0.0",
                },
            },
            headers=admin_headers,
        )
        resp = client.get("/api/v1/sources", headers=admin_headers)
        by_name = {item["name"]: item for item in resp.json()["items"]}
        assert by_name["aws-own"]["current_table_topic_type"] == "own"

    def test_list_table_topic_type_tracks_the_working_current_version(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        # Deployed 1.0.0 owns a table; working current 2.0.0 dropped the meta schema.
        client.post(
            "/api/v1/sources",
            json={
                **sample_source,
                "source": "azure-drift",
                "schema_config": {
                    "meta_schema": "meta/azure",
                    "meta_schema_version": "1.0.0",
                },
            },
            headers=admin_headers,
        )
        registry = _registries["source"]
        registry.set_deployed_version("azure-drift", "1.0.0")
        registry.save_source(
            {
                "source": "azure-drift",
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": sample_source["match"],
                        "schema": {
                            "meta_schema": "meta/azure",
                            "meta_schema_version": "1.0.0",
                        },
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "match": sample_source["match"],
                        "schema": {},
                    },
                },
            },
            created_by="test",
            description="e2e: current without meta schema",
        )

        resp = client.get("/api/v1/sources", headers=admin_headers)
        by_name = {item["name"]: item for item in resp.json()["items"]}
        assert by_name["azure-drift"]["current"] == "2.0.0"
        assert by_name["azure-drift"]["deployed_version"] == "1.0.0"
        assert by_name["azure-drift"]["current_table_topic_type"] == "main"

    def test_list_pagination(self, client: TestClient, admin_headers: dict):
        # Create 5 sources
        for i in range(5):
            client.post(
                "/api/v1/sources",
                json={
                    "source": f"src-{i}",
                    "enabled": True,
                    "match": {"field": "tags.collector.type", "value": f"src-{i}"},
                },
                headers=admin_headers,
            )

        # Six in total: the five above plus the seeded landing source.
        resp = client.get("/api/v1/sources?page=1&per_page=2", headers=admin_headers)
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 6
        assert data["total_pages"] == 3
        assert data["next_page"] == 2
        assert data["prev_page"] is None

        # Page 3 (last)
        resp = client.get("/api/v1/sources?page=3&per_page=2", headers=admin_headers)
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["next_page"] is None
        assert data["prev_page"] == 2

    def test_list_invalid_per_page_returns_422(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources?page=1&per_page=0", headers=admin_headers)
        assert resp.status_code == 422

    def test_list_search(self, client: TestClient, admin_headers: dict):
        client.post(
            "/api/v1/sources",
            json={
                "source": "windows-audit",
                "display_name": "Windows Audit Logs",
                "match": {"field": "tags.collector.type", "value": "windows-audit"},
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={
                "source": "linux-syslog",
                "display_name": "Linux Syslog",
                "match": {"field": "tags.collector.type", "value": "linux-syslog"},
            },
            headers=admin_headers,
        )

        resp = client.get("/api/v1/sources?search=windows", headers=admin_headers)
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "windows-audit"

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
        assert names[0] == "main"
        assert names[1:] == sorted(names[1:])

    def test_list_pins_main_first_regardless_of_sort(self, client: TestClient, admin_headers: dict):
        for name in ["zebra", "alpha"]:
            client.post(
                "/api/v1/sources",
                json={
                    "source": name,
                    "match": {"field": "tags.collector.type", "value": name},
                },
                headers=admin_headers,
            )

        resp = client.get("/api/v1/sources?sort_by=source&sort_order=asc", headers=admin_headers)
        names = [item["name"] for item in resp.json()["items"]]
        assert names[0] == "main"
        assert names[1:] == sorted(names[1:])

    def test_list_object_tree_places_sources_at_root(self, client: TestClient, admin_headers: dict):
        client.post(
            "/api/v1/sources",
            json={
                "source": "aws-cloudtrail",
                "display_name": "AWS CloudTrail",
                "match": {"field": "tags.collector.type", "value": "aws-cloudtrail"},
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={
                "source": "dfe-alerts",
                "display_name": "DFE Alerts",
                "match": {"field": "tags.collector.type", "value": "dfe-alerts"},
            },
            headers=admin_headers,
        )
        resp = client.get("/api/v1/sources", headers=admin_headers)
        data = resp.json()
        root_names = {item["name"] for item in data["objects"]["items"]}
        assert "aws-cloudtrail" in root_names
        assert "dfe-alerts" in root_names
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
                "source": "enabled-only-src",
                "enabled": True,
                "match": {"field": "tags.collector.type", "value": "enabled-only-src"},
            },
            headers=admin_headers,
        )
        client.post(
            "/api/v1/sources",
            json={
                "source": "disabled-only-src",
                "enabled": False,
                "match": {"field": "tags.collector.type", "value": "disabled-only-src"},
            },
            headers=admin_headers,
        )
        resp = client.get("/api/v1/sources?enabled=false", headers=admin_headers)
        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()["items"]]
        assert "disabled-only-src" in names
        assert "enabled-only-src" not in names


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
        assert data["source"] == "test-source"
        assert data["message"] == "created"
        assert data["current"] == "1.0.0"
        assert data["deployed_version"] is None
        assert data["versions"] == ["1.0.0"]

        get_resp = client.get("/api/v1/sources/test-source", headers=admin_headers)
        body = get_resp.json()
        assert body["current"] == "1.0.0"
        assert body["deployed_version"] is None
        assert "1.0.0" in body["versions"]

    def test_create_hyphenated_source_round_trips(self, client: TestClient, admin_headers: dict):
        """A hyphenated name has to survive creation, not merely pass the pattern.

        The live regression this pins: the name validated, then the create path
        raised a raw pydantic error nothing caught and the caller got a 500.
        """
        resp = client.post(
            "/api/v1/sources",
            json={
                "source": "verify-syslog",
                "match": {"field": "tags.collector.type", "value": "verify-syslog"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["source"] == "verify-syslog"

        body = client.get("/api/v1/sources/verify-syslog", headers=admin_headers)
        assert body.status_code == 200, body.text
        assert body.json()["source"] == "verify-syslog"
        # Derived from the name, so the hyphen is the word separator.
        assert body.json()["display_name"] == "Verify Syslog"

        listed = client.get("/api/v1/sources", headers=admin_headers).json()
        assert "verify-syslog" in [item["name"] for item in listed["items"]]

    def test_create_underscore_source_is_422_naming_the_constraint(
        self, client: TestClient, admin_headers: dict
    ):
        """An illegal name is a validation error, never an unhandled 500."""
        resp = client.post(
            "/api/v1/sources",
            json={
                "source": "verify_syslog",
                "match": {"field": "tags.collector.type", "value": "verify_syslog"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "validation_error"
        assert "DNS-1123 label" in body["message"]
        assert "use '-' instead of '_'" in body["message"]

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

    def test_create_rejects_removed_2_1_sigma_key(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        """A 2.1 body with the removed 'sigma' key fails loudly (422), never a
        silent 200 that drops the mapping config."""
        resp = client.post(
            "/api/v1/sources",
            json={**sample_source, "sigma": {"taxonomy": "windows"}},
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


class TestCreateOnATransportTheDeploymentLacks:
    """A source may name only what this deployment carries, and is told so at save."""

    @pytest.fixture(autouse=True)
    def _bus_deployment(self, monkeypatch, api_settings):
        # The save path reads the process-wide settings, so the deployment a
        # refusal is judged against is the one the app was built with.
        monkeypatch.setattr("dfe_engine.settings.get_settings", lambda: api_settings)

    def test_direct_on_a_bus_deployment_is_refused_with_the_reason(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        # The loader consumes the topic on a bus deployment, so a direct source
        # would compile an endpoint nothing serves and its records would land
        # nowhere. Refusing at save is where the person who typed it can see it.
        resp = client.post(
            "/api/v1/sources",
            json={**sample_source, "transport": "direct"},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "validation_error"
        assert "asks for the direct transport" in body["message"]
        assert "offers bus" in body["message"]

    def test_the_transport_the_deployment_carries_still_saves(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        resp = client.post(
            "/api/v1/sources",
            json={**sample_source, "transport": "bus"},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text


class TestGetSource:
    """GET /api/v1/sources/{name}"""

    def test_get_existing(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get("/api/v1/sources/test-source", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "test-source"
        assert data["display_name"] == "Test Source"
        ver = data["versions"]["1.0.0"]
        assert ver["origin"] == "receiver"
        assert ver["source_build"] is None
        assert ver["source_deployment"] is None

    def test_get_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources/nonexistent", headers=admin_headers)
        assert resp.status_code == 404


class TestMainFlow:
    """The landing table's source: seeded by the engine, read but never written."""

    def test_main_is_seeded_and_read_like_any_other_source(
        self, client: TestClient, admin_headers: dict
    ):
        resp = client.get("/api/v1/sources/main", headers=admin_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "main"
        assert body["resource_type"] == "core"
        # No match and no fetcher: the loader sends records here when it has nowhere else.
        assert body["match"] is None
        assert body["origin"] is None
        assert body["versions"][body["current"]]["origin"] is None
        assert body["transform"] is None
        assert body["archive"] is False

    def test_create_by_the_landing_name_is_refused(self, client: TestClient, admin_headers: dict):
        resp = client.post(
            "/api/v1/sources",
            json={"source": "main", "match": {"field": "tags.collector.type", "value": "main"}},
            headers=admin_headers,
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        stored = client.get("/api/v1/sources/main", headers=admin_headers).json()
        assert stored["resource_type"] == "core"
        assert stored["match"] is None

    def test_put_main_is_refused(self, client: TestClient, admin_headers: dict):
        resp = client.put(
            "/api/v1/sources/main",
            json={
                "match": {"field": "_source", "value": "main"},
                "description": "hijacked",
            },
            headers=admin_headers,
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert "main" in resp.json()["message"]
        stored = client.get("/api/v1/sources/main", headers=admin_headers).json()
        assert stored["description"] != "hijacked"

    def test_patch_main_is_refused(self, client: TestClient, admin_headers: dict):
        resp = client.patch("/api/v1/sources/main", json={"enabled": False}, headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        stored = client.get("/api/v1/sources/main", headers=admin_headers).json()
        assert stored["state"] == "active"

    def test_delete_main_is_refused(self, client: TestClient, admin_headers: dict):
        resp = client.delete("/api/v1/sources/main", headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert client.get("/api/v1/sources/main", headers=admin_headers).status_code == 200

    def test_bulk_delete_reports_main_as_failed_and_leaves_it(
        self, client: TestClient, admin_headers: dict
    ):
        client.post(
            "/api/v1/sources",
            json={"source": "spare", "match": {"field": "tags.collector.type", "value": "spare"}},
            headers=admin_headers,
        )

        resp = client.post(
            "/api/v1/sources/bulk",
            json={"action": "delete", "sources": ["main", "spare"]},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The refusal is per entry: the others in the same call still go.
        assert body["succeeded"] == ["spare"]
        assert [f["source"] for f in body["failed"]] == ["main"]
        # The same code the single-source DELETE answers with.
        assert body["failed"][0]["code"] == "conflict"
        assert "main" in body["failed"][0]["error"]
        assert client.get("/api/v1/sources/main", headers=admin_headers).status_code == 200

    def test_a_clone_of_main_is_refused(self, client: TestClient, admin_headers: dict):
        # A clone copies the body, which carries neither a match nor a fetcher.
        resp = client.post(
            "/api/v1/sources",
            json={"source": "main-copy", "description": "clone of the landing source"},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text

    def test_resource_type_in_a_write_body_is_refused(
        self, client: TestClient, admin_headers: dict
    ):
        resp = client.post(
            "/api/v1/sources",
            json={
                "source": "pretender",
                "resource_type": "core",
                "match": {"field": "_source", "value": "pretender"},
            },
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text

    def test_always_is_refused_on_any_other_source(self, client: TestClient, admin_headers: dict):
        resp = client.post(
            "/api/v1/sources",
            json={"source": "greedy", "match": {"field": "_source", "operator": "always"}},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert "reserved" in resp.json()["message"]


class TestGetSourceFlow:
    """GET /api/v1/sources/{name}/flow -- what the console draws."""

    def test_a_receiver_source_reports_its_match_and_where_it_lands(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)

        body = client.get("/api/v1/sources/test-source/flow", headers=admin_headers).json()

        assert body["source"] == "test-source"
        assert body["origin"] == "receiver"
        assert body["input"] == "tags.collector.type equals test_source"
        assert body["transport"] == "bus"
        assert body["carrier"] == "kafka"
        assert body["transform"] is None
        assert body["outputs"] == {"loader": "test-source_land", "archive": False}
        assert body["table"] == "test-source"

    def test_the_landing_flow_names_what_reaches_it(self, client: TestClient, admin_headers: dict):
        body = client.get("/api/v1/sources/main/flow", headers=admin_headers).json()

        assert body["source"] == "main"
        assert body["origin"] is None
        # Nothing selects these records; they arrived on some other path and went unclaimed.
        assert body["input"] == "records the loader could not route elsewhere"
        assert body["outputs"]["loader"] == "main_land"

    def test_an_unknown_source_has_no_flow(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources/nonexistent/flow", headers=admin_headers)

        assert resp.status_code == 404

    def test_a_flow_that_cannot_run_answers_with_the_reason(
        self, client: TestClient, admin_headers: dict, app, sample_source: dict
    ):
        # The console shows this message beside the choice that was refused, so it
        # has to say which stage refused and why, not just that something failed.
        # A source outlives the deployment shape it was written under: this one
        # names the bus, and the broker is gone by the time the flow is read.
        client.post(
            "/api/v1/sources",
            json={**sample_source, "transport": "bus"},
            headers=admin_headers,
        )
        app.state.settings = app.state.settings.model_copy(
            update={
                "transport": app.state.settings.transport.model_copy(
                    update={"default": "direct", "bus_present": False}
                )
            }
        )

        resp = client.get("/api/v1/sources/test-source/flow", headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert "asks for the bus transport" in resp.json()["message"]

    def test_reading_a_flow_needs_source_read(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)

        assert client.get("/api/v1/sources/test-source/flow").status_code == 401


class TestGetSourceVersion:
    """GET /api/v1/sources/{name}/versions/{version}"""

    def test_get_version_after_create(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.get(
            "/api/v1/sources/test-source/versions/1.0.0",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "test-source"
        assert body["selected"] == "1.0.0"
        assert body["current"] == "1.0.0"
        assert body["versions"] == ["1.0.0"]
        assert body["previous_deployed_versions"] == []
        assert body["current_table_topic_type"] == "main"
        assert body["version"]["schema"]["engine"] == "MergeTree"
        assert body["version"]["origin"] == "receiver"
        assert body["version"]["source_build"] is None
        assert body["version"]["source_deployment"] is None

    def test_get_version_reports_own_when_current_pins_meta_schema(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post(
            "/api/v1/sources",
            json={
                **sample_source,
                "schema_config": {
                    "meta_schema": "meta/aws_cloudtrail",
                    "meta_schema_version": "1.0.0",
                },
            },
            headers=admin_headers,
        )
        resp = client.get(
            "/api/v1/sources/test-source/versions/1.0.0",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["current_table_topic_type"] == "own"

    def test_get_version_table_topic_type_tracks_working_current(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        # Selected 1.0.0 owns a table; working current 2.0.0 does not.
        client.post(
            "/api/v1/sources",
            json={
                **sample_source,
                "schema_config": {
                    "meta_schema": "meta/azure",
                    "meta_schema_version": "1.0.0",
                },
            },
            headers=admin_headers,
        )
        registry = _registries["source"]
        registry.set_deployed_version("test-source", "1.0.0")
        registry.save_source(
            {
                "source": "test-source",
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "match": sample_source["match"],
                        "schema": {
                            "meta_schema": "meta/azure",
                            "meta_schema_version": "1.0.0",
                        },
                    },
                    "2.0.0": {
                        "date_time": "2026-01-02",
                        "match": sample_source["match"],
                        "schema": {},
                    },
                },
            },
            created_by="test",
            description="current without meta schema",
        )

        resp = client.get(
            "/api/v1/sources/test-source/versions/1.0.0",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["selected"] == "1.0.0"
        assert body["current"] == "2.0.0"
        assert body["current_table_topic_type"] == "main"

    def test_get_version_after_update_preserves_history(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        client.put(
            "/api/v1/sources/test-source",
            json={**sample_source, "description": "v2"},
            headers=admin_headers,
        )
        v1 = client.get(
            "/api/v1/sources/test-source/versions/1.0.0",
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
        registry.set_deployed_version("test-source", "1.0.0")

        client.put(
            "/api/v1/sources/test-source",
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
            "/api/v1/sources/test-source/versions/2.0.0",
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
            "/api/v1/sources/test-source/versions/9.9.9",
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
        resp = client.get("/api/v1/sources/test-source/versions", headers=admin_headers)
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
            "/api/v1/sources/test-source/versions/1.0.0",
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
            "/api/v1/sources/test-source",
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
        get_resp = client.get("/api/v1/sources/test-source", headers=admin_headers)
        assert get_resp.json()["description"] == "Updated description"
        body = get_resp.json()
        assert "1.0.0" in body["versions"]
        assert "2.0.0" not in body["versions"]
        assert body["current"] == "1.0.0"
        assert body["versions"]["1.0.0"]["schema"]["engine"] == "MergeTree"

    def test_update_refuses_to_swap_origin(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.put(
            "/api/v1/sources/test-source",
            json={
                "source": "test-source",
                "fetcher": {"source_type": "okta", "topic": "own"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        assert "Cannot change source origin" in resp.json()["message"]

    def test_update_refuses_to_clear_a_meta_schema(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post(
            "/api/v1/sources",
            json={
                **sample_source,
                "schema_config": {
                    "meta_schema": "meta/aws_cloudtrail",
                    "meta_schema_version": "1.0.0",
                },
            },
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/sources/test-source",
            json={**sample_source, "schema_config": {"engine": "MergeTree"}},
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        assert "Cannot remove the meta schema" in resp.json()["message"]

    def test_update_rejects_versions_payload(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.put(
            "/api/v1/sources/test-source",
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
            raise SourceNotFoundError("test-source")

        monkeypatch.setattr(registry, "update_source_from_write", missing)
        resp = client.put(
            "/api/v1/sources/test-source",
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
            "source": "source-alpha",
            "match": shared,
            "views": [],
        }
        second = {
            **sample_source,
            "source": "source-beta",
            "match": {"field": "ingest_type", "value": "other_value"},
            "views": [],
        }
        assert client.post("/api/v1/sources", json=first, headers=admin_headers).status_code == 201
        assert client.post("/api/v1/sources", json=second, headers=admin_headers).status_code == 201

        create_dup = client.post(
            "/api/v1/sources",
            json={**second, "source": "source-gamma", "match": shared},
            headers=admin_headers,
        )
        assert create_dup.status_code == 409
        dup_body = create_dup.json()
        assert dup_body["code"] == "match_conflict"
        assert dup_body["context"]["conflicting_source"] == "source-alpha"
        assert dup_body["context"]["source"] == "source-gamma"
        assert dup_body["context"]["field"] == "ingest_type"
        assert dup_body["context"]["value"] == "shared_value"
        assert "source-alpha" in dup_body["message"]

        conflict_put = client.put(
            "/api/v1/sources/source-beta",
            json={**second, "match": shared},
            headers=admin_headers,
        )
        assert conflict_put.status_code == 409
        assert conflict_put.json()["code"] == "match_conflict"


class TestDeleteSource:
    """DELETE /api/v1/sources/{name}"""

    def test_delete_success(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.delete("/api/v1/sources/test-source", headers=admin_headers)
        assert resp.status_code == 204

        # Verify it's gone
        get_resp = client.get("/api/v1/sources/test-source", headers=admin_headers)
        assert get_resp.status_code == 404

    def test_delete_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.delete("/api/v1/sources/nonexistent", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_viewer_forbidden(
        self, client: TestClient, viewer_headers: dict, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.delete("/api/v1/sources/test-source", headers=viewer_headers)
        assert resp.status_code == 403

    def test_delete_takes_the_source_topics_with_it(
        self, client: TestClient, admin_headers: dict, app, sample_source: dict, monkeypatch
    ):
        # Left behind, the pair costs a partition assignment in every loader for a
        # source nothing can write to, and the loader keeps resolving the name.
        removed: list[str] = []
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.remove_topics",
            lambda names, **kw: _removed(removed, names),
        )
        app.state.settings = app.state.settings.model_copy(
            update={"kafka": app.state.settings.kafka.model_copy(update={"ensure_topics": True})}
        )
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)

        assert (
            client.delete("/api/v1/sources/test-source", headers=admin_headers).status_code == 204
        )
        assert removed == ["test-source_land"]

    def test_bulk_delete_takes_each_source_topics_with_it(
        self, client: TestClient, admin_headers: dict, app, monkeypatch
    ):
        removed: list[str] = []
        monkeypatch.setattr(
            "dfe_engine.kafka.topics.remove_topics",
            lambda names, **kw: _removed(removed, names),
        )
        app.state.settings = app.state.settings.model_copy(
            update={"kafka": app.state.settings.kafka.model_copy(update={"ensure_topics": True})}
        )
        for name in ("bulk-x", "bulk-y"):
            client.post(
                "/api/v1/sources",
                json={"source": name, "match": {"field": "tags.collector.type", "value": name}},
                headers=admin_headers,
            )

        resp = client.post(
            "/api/v1/sources/bulk",
            json={"action": "delete", "sources": ["bulk-x", "bulk-y"]},
            headers=admin_headers,
        )

        assert resp.status_code == 200
        assert removed == ["bulk-x_land", "bulk-y_land"]


class TestPatchSourceEnabled:
    """PATCH /api/v1/sources/{name}"""

    def test_disable_and_enable(self, client: TestClient, admin_headers: dict, sample_source: dict):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)

        disable = client.patch(
            "/api/v1/sources/test-source",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert disable.status_code == 200
        assert disable.json()["message"] == "disabled"
        assert (
            client.get("/api/v1/sources/test-source", headers=admin_headers).json()["enabled"]
            is False
        )

        enable = client.patch(
            "/api/v1/sources/test-source",
            json={"enabled": True},
            headers=admin_headers,
        )
        assert enable.status_code == 200
        assert enable.json()["message"] == "active"
        assert (
            client.get("/api/v1/sources/test-source", headers=admin_headers).json()["enabled"]
            is True
        )

    def test_patch_state_dormant(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test-source",
            json={"state": "dormant"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "dormant"
        detail = client.get("/api/v1/sources/test-source", headers=admin_headers).json()
        assert detail["state"] == "dormant"
        assert detail["enabled"] is False  # compat accessor

    def test_patch_requires_state_or_enabled(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test-source",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_idempotent_when_already_enabled(
        self, client: TestClient, admin_headers: dict, sample_source: dict
    ):
        client.post("/api/v1/sources", json=sample_source, headers=admin_headers)
        resp = client.patch(
            "/api/v1/sources/test-source",
            json={"enabled": True},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "active"

    def test_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.patch(
            "/api/v1/sources/missing-src",
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
            "/api/v1/sources/test-source",
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
                "source": "bulk-toggle",
                "match": {"field": "tags.collector.type", "value": "bulk-toggle"},
            },
            headers=admin_headers,
        )
        disable = client.post(
            "/api/v1/sources/bulk",
            json={"action": "disable", "sources": ["bulk-toggle"]},
            headers=admin_headers,
        )
        assert disable.status_code == 200
        assert disable.json()["succeeded"] == ["bulk-toggle"]
        assert (
            client.get("/api/v1/sources/bulk-toggle", headers=admin_headers).json()["enabled"]
            is False
        )

        enable = client.post(
            "/api/v1/sources/bulk",
            json={"action": "enable", "sources": ["bulk-toggle"]},
            headers=admin_headers,
        )
        assert enable.status_code == 200
        assert enable.json()["succeeded"] == ["bulk-toggle"]
        assert (
            client.get("/api/v1/sources/bulk-toggle", headers=admin_headers).json()["enabled"]
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


class TestDeployReportsWhyItCouldNotBuild:
    """A schema the deploy cannot LOAD is the caller's fault, not the server's.

    `SchemaBuildError` was mapped to a 400 and `SchemaLoadError` was not, so a
    source pinned to a derived-schema version that no longer exists answered
    `500 internal_error` while the engine's own log named the missing version.
    """

    def test_a_schema_that_cannot_be_loaded_answers_400_with_the_reason(
        self,
        client: TestClient,
        admin_headers: dict,
        sample_source: dict,
        monkeypatch,
    ):
        from dfe_engine.schema.schema_loader import SchemaLoadError

        client.post(
            "/api/v1/sources",
            json={**sample_source, "schema": {"meta_schema": "meta/beats/filebeat"}},
            headers=admin_headers,
        )

        def _raise(*args, **kwargs):
            raise SchemaLoadError("Version '1.0.0' not found in derived/accept/gone.yaml")

        monkeypatch.setattr(
            "dfe_engine.schema.schema_builder_v2.SchemaBuilderV2.build_for_source_version",
            _raise,
        )
        resp = client.post(
            f"/api/v1/sources/{sample_source['source']}/deploy",
            headers=admin_headers,
        )

        assert resp.status_code == 400, resp.text
        assert "not found" in resp.json()["message"]

    def test_a_meta_schema_that_does_not_exist_answers_400_on_the_real_path(
        self, client, admin_headers, sample_source
    ):
        """The same refusal, driven rather than injected.

        The faked version above patches the builder, so it proves the deploy
        route's handler. This one proves an absent meta_schema actually reaches
        it: `schema_builder_v2.py:373` re-raises a SchemaLoadError as
        SchemaBuildError, and the handler at `api/v1/sources.py:1394` catches
        both, so only a real load shows which class arrives.
        """
        client.post(
            "/api/v1/sources",
            json={**sample_source, "schema": {"meta_schema": "meta/beats/nosuchthing"}},
            headers=admin_headers,
        )

        resp = client.post(
            f"/api/v1/sources/{sample_source['source']}/deploy",
            headers=admin_headers,
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "build_error"
