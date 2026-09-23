#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_derived_schemas.py
#  Purpose:      Tests for the derived-schema API
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Create, read, update and delete a column selection over a meta schema.

The three refusals are the point of the layer: a name the base does not define,
an index the type registry does not know, and an index the column's primitive
cannot take. Each names the column, so a caller can fix the one row at fault.
"""

from __future__ import annotations

import pytest

BASE = "meta/beats/filebeat"
DERIVED = "beats/filebeat_auth"
URL = f"/api/v1/schemas/definitions/derived/{DERIVED}"


def _column(name: str, type_: str, use_case: str | None = None) -> dict:
    column = {"name": name, "type": type_, "_field_type": "base"}
    if use_case:
        column["use_case"] = use_case
    return column


BASE_COLUMNS = [
    _column("timestamp", "timestamp", "range"),
    _column("host_name", "string", "dimension"),
    _column("message", "text"),
    _column("log_offset", "integer", "range"),
]


def _body(select: list[dict], **version_keys) -> dict:
    return {
        "path": f"derived/{DERIVED}",
        "base": BASE,
        "base_version": "1.0.0",
        "current": "1.0.0",
        "versions": {
            "1.0.0": {
                "date": "2026-09-21",
                "summary": "system.auth subset",
                "select": select,
                **version_keys,
            }
        },
    }


@pytest.fixture
def base_schema(client, admin_headers):
    """The meta schema every selection below is taken from."""
    response = client.post(
        f"/api/v1/schemas/definitions/{BASE}",
        headers=admin_headers,
        json={
            "current": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date": "2026-09-21",
                    "type": "model",
                    "summary": "filebeat base",
                    "columns": BASE_COLUMNS,
                }
            },
        },
    )
    assert response.status_code == 201, response.text
    return BASE


class TestCreate:
    def test_a_selection_is_stored_and_read_back(self, base_schema, client, admin_headers):
        created = client.post(
            URL,
            headers=admin_headers,
            json=_body([{"name": "timestamp"}, {"name": "host_name", "index": "exact_match"}]),
        )
        assert created.status_code == 201, created.text
        assert created.json()["path"] == f"derived/{DERIVED}"

        read = client.get(URL, headers=admin_headers)
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["base"] == BASE
        assert body["versions"]["1.0.0"]["select"] == [
            {"name": "timestamp", "index": None},
            {"name": "host_name", "index": "exact_match"},
        ]

    def test_the_catch_all_switches_default_on(self, base_schema, client, admin_headers):
        client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        version = client.get(URL, headers=admin_headers).json()["versions"]["1.0.0"]
        assert (version["capture_json"], version["capture_raw"]) == (True, True)

    def test_stage_three_turns_them_off(self, base_schema, client, admin_headers):
        created = client.post(
            URL,
            headers=admin_headers,
            json=_body([{"name": "timestamp"}], capture_json=False, capture_raw=False),
        )
        assert created.status_code == 201, created.text
        version = created.json()["versions"]["1.0.0"]
        assert (version["capture_json"], version["capture_raw"]) == (False, False)

    def test_a_second_create_at_the_same_path_is_refused(self, base_schema, client, admin_headers):
        client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        again = client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        assert again.status_code == 422
        assert "already exists" in again.json()["message"]

    def test_a_body_path_that_disagrees_with_the_url_is_refused(
        self, base_schema, client, admin_headers
    ):
        body = {**_body([{"name": "timestamp"}]), "path": "derived/beats/something_else"}
        response = client.post(URL, headers=admin_headers, json=body)
        assert response.status_code == 422
        assert response.json()["code"] == "path_mismatch"

    def test_the_derived_route_is_not_swallowed_by_the_meta_schema_catch_all(
        self, base_schema, client, admin_headers
    ):
        # Both routes are POST /definitions/{path}; the derived router is mounted
        # first so a selection body is not read as a meta-schema body.
        created = client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        assert created.status_code == 201, created.text
        listed = client.get("/api/v1/schemas", headers=admin_headers, params={"per_page": -1})
        assert f"derived/{DERIVED}" not in [row["name"] for row in listed.json()["items"]]


class TestValidationFailures:
    def test_a_name_the_base_does_not_define(self, base_schema, client, admin_headers):
        response = client.post(URL, headers=admin_headers, json=_body([{"name": "user_name"}]))
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "unknown_column"
        assert "user_name" in body["message"]
        assert BASE in body["message"]

    def test_an_index_the_type_registry_does_not_know(self, base_schema, client, admin_headers):
        response = client.post(
            URL,
            headers=admin_headers,
            json=_body([{"name": "message", "index": "fulltext"}]),
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "unknown_index_use_case"
        assert "word_search" in body["message"]

    def test_an_index_the_primitive_cannot_take(self, base_schema, client, admin_headers):
        response = client.post(
            URL,
            headers=admin_headers,
            json=_body([{"name": "log_offset", "index": "word_search"}]),
        )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "incompatible_index"
        assert "integer" in body["message"]

    def test_an_unknown_base(self, base_schema, client, admin_headers):
        body = {**_body([{"name": "timestamp"}]), "base": "meta/beats/absent"}
        response = client.post(URL, headers=admin_headers, json=body)
        assert response.status_code == 422
        assert response.json()["code"] == "unknown_base"

    def test_a_base_version_the_base_does_not_carry(self, base_schema, client, admin_headers):
        body = {**_body([{"name": "timestamp"}]), "base_version": "9.9.9"}
        response = client.post(URL, headers=admin_headers, json=body)
        assert response.status_code == 422
        assert response.json()["code"] == "unknown_base_version"

    def test_capture_json_alone_is_refused(self, base_schema, client, admin_headers):
        response = client.post(
            URL,
            headers=admin_headers,
            json=_body([{"name": "timestamp"}], capture_json=True, capture_raw=False),
        )
        assert response.status_code == 422
        assert "dfe-loader cannot express" in response.text


class TestUpdateAndDelete:
    def test_a_put_replaces_the_selection(self, base_schema, client, admin_headers):
        client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        updated = client.put(
            URL,
            headers=admin_headers,
            json=_body([{"name": "host_name"}, {"name": "message", "index": "word_search"}]),
        )
        assert updated.status_code == 200, updated.text
        assert [entry["name"] for entry in updated.json()["versions"]["1.0.0"]["select"]] == [
            "host_name",
            "message",
        ]

    def test_a_put_is_validated_like_a_create(self, base_schema, client, admin_headers):
        client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        response = client.put(URL, headers=admin_headers, json=_body([{"name": "nope"}]))
        assert response.status_code == 422
        assert response.json()["code"] == "unknown_column"

    def test_a_put_to_a_path_with_nothing_there(self, base_schema, client, admin_headers):
        response = client.put(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        assert response.status_code == 404

    def test_delete_removes_it(self, base_schema, client, admin_headers):
        client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        assert client.delete(URL, headers=admin_headers).status_code == 204
        assert client.get(URL, headers=admin_headers).status_code == 404
        assert client.delete(URL, headers=admin_headers).status_code == 404


class TestList:
    def test_the_listing_carries_the_base_and_the_column_count(
        self, base_schema, client, admin_headers
    ):
        client.post(
            URL,
            headers=admin_headers,
            json=_body([{"name": "timestamp"}, {"name": "host_name"}]),
        )
        listed = client.get("/api/v1/schemas/definitions/derived", headers=admin_headers)
        assert listed.status_code == 200, listed.text
        rows = listed.json()["items"]
        assert len(rows) == 1
        assert rows[0]["path"] == f"derived/{DERIVED}"
        assert rows[0]["base"] == BASE
        assert rows[0]["column_count"] == 2


class TestAuthorisation:
    def test_a_viewer_may_read_but_not_write(
        self, base_schema, client, admin_headers, viewer_headers
    ):
        client.post(URL, headers=admin_headers, json=_body([{"name": "timestamp"}]))
        assert client.get(URL, headers=viewer_headers).status_code == 200
        assert (
            client.post(
                "/api/v1/schemas/definitions/derived/beats/other",
                headers=viewer_headers,
                json=_body([{"name": "timestamp"}]),
            ).status_code
            == 403
        )
