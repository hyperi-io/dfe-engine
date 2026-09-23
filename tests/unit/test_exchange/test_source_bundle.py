#  Project:      dfe-engine
#  File:         tests/unit/test_exchange/test_source_bundle.py
#  Purpose:      Source bundle export and import across two deployments
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A bundle exported from one deployment, imported into a clean one.

alpha carries the operator's schema and source; beta carries only the
pre-supplied schema dfe-schemas ships, as a fresh deployment does.
"""

from __future__ import annotations

from typing import Any

from tests.unit.test_exchange.conftest import CORE_SCHEMA, CUSTOM_SCHEMA

SOURCE_BODY = {
    "source": "cisco-ios",
    "display_name": "Cisco IOS",
    "description": "Cisco IOS syslog",
    "match": {"field": "tags.collector.type", "value": "cisco_ios"},
    "header": {"type": "timeseries", "version": "1.0.0"},
    "schema": {
        "meta_schema": "meta/cisco_ios",
        "meta_schema_version": "1.1.0",
        "additional_fields": "meta/core_tpl",
        "engine": "MergeTree",
    },
    "transform": {"engine": "vector", "config_file": "/etc/vector/cisco-ios.toml"},
}

CORE_PINNED_BODY = {
    **SOURCE_BODY,
    "schema": {
        "meta_schema": "meta/core_tpl",
        "meta_schema_version": "2.0.0",
        "engine": "MergeTree",
    },
}
"""A source whose only pin is the pre-supplied schema, which travels as a reference."""


def _export_from_alpha(deployment, *, source_body: dict[str, Any] | None = None) -> dict:
    """Stand up the authoring deployment, create the source, return its bundle."""
    client, headers = deployment(
        "alpha", schemas={"meta/cisco_ios": CUSTOM_SCHEMA, "meta/core_tpl": CORE_SCHEMA}
    )
    with client:
        created = client.post("/api/v1/sources", json=source_body or SOURCE_BODY, headers=headers)
        assert created.status_code == 201, created.text
        exported = client.get("/api/v1/sources/cisco-ios/export", headers=headers)
        assert exported.status_code == 200, exported.text
        return exported.json()


class TestBundleShape:
    def test_it_carries_routing_schema_and_transform(self, deployment):
        bundle = _export_from_alpha(deployment)

        assert bundle["routing"] == {
            "field": "tags.collector.type",
            "operator": "equals",
            "value": "cisco_ios",
        }
        assert bundle["schema"]["pins"]["meta_schema"] == "meta/cisco_ios"
        assert bundle["schema"]["pins"]["meta_schema_version"] == "1.1.0"
        assert bundle["schema"]["header"] == {"type": "timeseries", "version": "1.0.0"}
        assert bundle["transform"]["engine"] == "vector"
        assert bundle["version"] == "1.0.0"

    def test_it_carries_a_document_for_every_pin(self, deployment):
        bundle = _export_from_alpha(deployment)

        definitions = {doc["path"]: doc for doc in bundle["schema"]["definitions"]}

        assert sorted(definitions) == ["meta/cisco_ios", "meta/core_tpl"]
        assert definitions["meta/cisco_ios"]["resource_type"] == "custom"
        assert sorted(definitions["meta/cisco_ios"]["versions"]) == ["1.0.0", "1.1.0"]

    def test_the_core_pin_travels_as_a_reference_not_a_copy(self, deployment):
        bundle = _export_from_alpha(deployment)

        core = next(
            doc for doc in bundle["schema"]["definitions"] if doc["path"] == "meta/core_tpl"
        )

        assert core["resource_type"] == "core"
        assert core["reference"] == {"path": "meta/core_tpl", "version": "2.0.0"}
        assert "versions" not in core
        assert "observed_at" not in str(core)

    def test_a_source_with_no_transform_carries_no_transform_section(self, deployment):
        body = {key: value for key, value in SOURCE_BODY.items() if key != "transform"}

        bundle = _export_from_alpha(deployment, source_body=body)

        assert "transform" not in bundle


class TestRoundTripIntoACleanDeployment:
    def test_the_source_arrives_with_its_routing_schema_and_transform(self, deployment):
        bundle = _export_from_alpha(deployment)

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            assert imported.status_code == 201, imported.text
            assert imported.json()["action"] == "created"
            assert imported.json()["version"] == "1.0.0"

            landed = client.get("/api/v1/sources/cisco-ios", headers=headers)
            assert landed.status_code == 200
            source = landed.json()

        assert source["match"] == bundle["routing"]
        assert source["transform"]["engine"] == "vector"
        assert source["transform"]["config_file"] == "/etc/vector/cisco-ios.toml"
        assert source["versions"]["1.0.0"]["schema"]["meta_schema"] == "meta/cisco_ios"
        assert source["versions"]["1.0.0"]["schema"]["meta_schema_version"] == "1.1.0"
        assert source["versions"]["1.0.0"]["schema"]["additional_fields"] == "meta/core_tpl"

    def test_the_custom_schema_arrives_and_the_core_one_is_not_forked(self, deployment):
        bundle = _export_from_alpha(deployment)

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            assert imported.status_code == 201, imported.text
            actions = {entry["path"]: entry["action"] for entry in imported.json()["schemas"]}

            listed = client.get("/api/v1/schemas?per_page=-1", headers=headers)
            paths = {entry["name"] for entry in listed.json()["items"]}
            columns = client.get(
                "/api/v1/schemas/definitions/meta/cisco_ios/versions/columns?version=1.1.0",
                headers=headers,
            )

        assert actions == {"meta/cisco_ios": "created", "meta/core_tpl": "resolved"}
        # A fork would show up as a per-source copy of the core path; neither exists.
        assert {"meta/cisco_ios", "meta/core_tpl"} <= paths
        assert not [path for path in paths if path.startswith("meta/cisco-ios_")]
        assert columns.status_code == 200
        assert [column["name"] for column in columns.json()["version"]["columns"]["items"]] == [
            "host_name",
            "event_code",
        ]

    def test_a_second_import_is_refused(self, deployment):
        bundle = _export_from_alpha(deployment)

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            first = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            again = client.post("/api/v1/sources/import", json=bundle, headers=headers)

        assert first.status_code == 201
        assert again.status_code == 409
        assert again.json()["code"] == "conflict"

    def test_a_missing_core_schema_refuses_the_whole_bundle(self, deployment):
        bundle = _export_from_alpha(deployment)

        client, headers = deployment("beta", schemas={})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            listed = client.get("/api/v1/sources", headers=headers)
            schemas = client.get("/api/v1/schemas?per_page=-1", headers=headers)

        assert imported.status_code == 422
        assert imported.json()["code"] == "unresolved_reference"
        # Nothing was written: the checks run before the first write.
        assert "cisco-ios" not in {entry["name"] for entry in listed.json()["items"]}
        assert "meta/cisco_ios" not in {entry["name"] for entry in schemas.json()["items"]}


class TestAPinWithNoDefinition:
    """The import mirrors the export: a pin resolves, or the whole bundle is refused."""

    def test_a_pin_neither_side_carries_refuses_the_whole_bundle(self, deployment):
        bundle = _export_from_alpha(deployment)
        bundle["schema"]["definitions"] = []

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            listed = client.get("/api/v1/sources", headers=headers)
            schemas = client.get("/api/v1/schemas?per_page=-1", headers=headers)

        assert imported.status_code == 422, imported.text
        assert imported.json()["code"] == "unresolved_reference"
        assert "meta/cisco_ios" in imported.json()["message"]
        # Nothing was written: the check runs before the first write.
        assert "cisco-ios" not in {entry["name"] for entry in listed.json()["items"]}
        assert "meta/cisco_ios" not in {entry["name"] for entry in schemas.json()["items"]}

    def test_a_core_pin_the_target_already_holds_is_accepted(self, deployment):
        bundle = _export_from_alpha(deployment, source_body=CORE_PINNED_BODY)
        bundle["schema"]["definitions"] = []

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            landed = client.get("/api/v1/sources/cisco-ios", headers=headers)

        assert imported.status_code == 201, imported.text
        assert landed.json()["versions"]["1.0.0"]["schema"]["meta_schema"] == "meta/core_tpl"

    def test_a_pin_the_bundle_defines_is_accepted(self, deployment):
        bundle = _export_from_alpha(deployment)

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            schemas = client.get("/api/v1/schemas?per_page=-1", headers=headers)

        assert imported.status_code == 201, imported.text
        assert "meta/cisco_ios" in {entry["name"] for entry in schemas.json()["items"]}


class TestCoreSourceTravelsAsAReference:
    """``main`` is the landing source the engine reconciles from its own settings."""

    def test_the_export_carries_no_definition(self, deployment):
        client, headers = deployment("alpha", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            exported = client.get("/api/v1/sources/main/export", headers=headers)

        assert exported.status_code == 200, exported.text
        bundle = exported.json()
        assert bundle["resource_type"] == "core"
        assert bundle["reference"] == {"path": "main", "version": "1.0.1"}
        assert "routing" not in bundle
        assert "schema" not in bundle
        assert "transform" not in bundle

    def test_the_import_resolves_against_the_engine_owned_source(self, deployment):
        client, headers = deployment("alpha", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            bundle = client.get("/api/v1/sources/main/export", headers=headers).json()

        client, headers = deployment("beta", schemas={"meta/core_tpl": CORE_SCHEMA})
        with client:
            imported = client.post("/api/v1/sources/import", json=bundle, headers=headers)
            landed = client.get("/api/v1/sources/main", headers=headers)

        assert imported.status_code == 201, imported.text
        assert imported.json()["action"] == "resolved"
        # Still the engine's, not an operator copy the import forked.
        assert landed.json()["resource_type"] == "core"
