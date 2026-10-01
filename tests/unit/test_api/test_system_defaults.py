#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_defaults.py
#  Purpose:      GET/PATCH /api/v1/system/defaults short of a live schema apply
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Who may read and patch the table defaults, and which fields reach ClickHouse.

Header type, header version and engine are stored for the next deploy. Only a
patch that names ttl_days reconciles live TTL, the same way PUT /retention does.
"""

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import ClickHouseResilienceSettings

URL = "/api/v1/system/defaults"


def _wire(app, tmp_path) -> GitCrud:
    gc = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = gc
    return gc


def _deployment_body(*, editable: bool) -> dict:
    return {
        "ttl_days": {
            "effective": 90,
            "stored": None,
            "origin": "deployment",
            "deployment_default": 90,
        },
        "common_header_type": {
            "effective": "timeseries",
            "stored": None,
            "origin": "deployment",
            "deployment_default": "timeseries",
        },
        "common_header_version": {
            "effective": "1.0.0",
            "stored": None,
            "origin": "deployment",
            "deployment_default": "1.0.0",
        },
        "engine": {
            "effective": "MergeTree",
            "stored": None,
            "origin": "deployment",
            "deployment_default": "MergeTree",
        },
        "editable": editable,
    }


class TestGetDefaults:
    def test_reports_the_deployment_values_without_gitops(self, admin_headers, client):
        resp = client.get(URL, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == _deployment_body(editable=False)

    def test_a_viewer_cannot_read_them(self, client, viewer_headers):
        assert client.get(URL, headers=viewer_headers).status_code == 403


class TestPatchDefaults:
    @pytest.fixture
    def unreachable_clickhouse(self, api_settings):
        api_settings.clickhouse = api_settings.clickhouse.model_copy(
            update={
                "host": "127.0.0.1",
                "port": 1,
                "secure": False,
                "resilience": ClickHouseResilienceSettings(enabled=False),
            }
        )
        ClickHouseManager.reset_instance()
        yield
        ClickHouseManager.reset_instance()

    def test_a_partial_patch_stores_only_the_named_fields(
        self, unreachable_clickhouse, admin_headers, app, client, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = client.patch(
            URL,
            headers=admin_headers,
            json={"common_header_type": "minimal", "engine": "ReplacingMergeTree"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["common_header_type"]["effective"] == "minimal"
        assert body["common_header_type"]["stored"] == "minimal"
        assert body["common_header_type"]["origin"] == "override"
        assert body["common_header_version"]["stored"] is None
        assert body["engine"]["effective"] == "ReplacingMergeTree"
        assert body["engine"]["origin"] == "override"
        assert body["ttl_days"]["stored"] is None
        assert body["reconcile"] is None
        assert body["editable"] is True
        assert gc.get("gov_settings", "defaults") == {
            "common_header_type": "minimal",
            "default_engine": "ReplacingMergeTree",
        }
        assert client.get("/api/v1/auth/setup-status").json()["default_engine"] == (
            "ReplacingMergeTree"
        )

    def test_null_clears_one_override(self, admin_headers, app, client, tmp_path):
        gc = _wire(app, tmp_path)
        client.patch(
            URL,
            headers=admin_headers,
            json={"engine": "ReplacingMergeTree", "common_header_type": "minimal"},
        )

        resp = client.patch(URL, headers=admin_headers, json={"engine": None})

        assert resp.status_code == 200, resp.text
        assert resp.json()["engine"]["stored"] is None
        assert resp.json()["engine"]["effective"] == "MergeTree"
        assert resp.json()["common_header_type"]["stored"] == "minimal"
        assert "default_engine" not in gc.get("gov_settings", "defaults")

    def test_an_empty_patch_commits_nothing(self, admin_headers, app, client, tmp_path):
        gc = _wire(app, tmp_path)

        resp = client.patch(URL, headers=admin_headers, json={})

        assert resp.status_code == 200, resp.text
        assert resp.json()["reconcile"] is None
        assert gc.head_revision() is None

    def test_ttl_is_the_retention_override_and_a_clickhouse_failure_keeps_it(
        self, unreachable_clickhouse, admin_headers, app, client, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = client.patch(
            URL,
            headers=admin_headers,
            json={"ttl_days": 30, "engine": "ReplacingMergeTree"},
        )

        assert resp.status_code == 502, resp.text
        assert resp.json()["code"] == "reconcile_failed"
        assert gc.get("gov_settings", "retention")["default_ttl_days"] == 30
        assert gc.get("gov_settings", "defaults")["default_engine"] == "ReplacingMergeTree"
        assert client.get(URL, headers=admin_headers).json()["ttl_days"]["origin"] == "override"

    @pytest.mark.parametrize("who", ["viewer_headers", "operator_headers"])
    def test_a_non_admin_is_refused_and_nothing_is_committed(
        self, app, client, request, tmp_path, who
    ):
        gc = _wire(app, tmp_path)

        resp = client.patch(
            URL, headers=request.getfixturevalue(who), json={"engine": "ReplacingMergeTree"}
        )

        assert resp.status_code == 403, resp.text
        assert gc.head_revision() is None

    @pytest.mark.parametrize(
        "body",
        [
            {"ttl_days": -1},
            {"ttl_days": "30"},
            {"engine": "Log"},
            {"engine": ""},
            {"common_header_type": "no-such-profile"},
            {"common_header_version": "9.9.9"},
        ],
    )
    def test_an_invalid_value_is_422_and_nothing_is_committed(
        self, admin_headers, app, body, client, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = client.patch(URL, headers=admin_headers, json=body)

        assert resp.status_code == 422, resp.text
        assert gc.head_revision() is None

    def test_without_gitops_there_is_nowhere_to_store_them(self, admin_headers, client):
        resp = client.patch(URL, headers=admin_headers, json={"engine": "ReplacingMergeTree"})

        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "not_configured"


APPLY = "/api/v1/system/defaults/apply"


def _create_source(client, headers, name: str) -> None:
    resp = client.post(
        "/api/v1/sources",
        headers=headers,
        json={"source": name, "match": {"field": "tags.collector.type", "value": name}},
    )
    assert resp.status_code == 201, resp.text


def _version(client, headers, name: str) -> dict:
    resp = client.get(f"/api/v1/sources/{name}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["versions"]["1.0.0"]


class TestApplyDefaults:
    def test_only_the_named_sources_are_pinned(self, admin_headers, app, client, tmp_path):
        _wire(app, tmp_path)
        patched = client.patch(
            URL,
            headers=admin_headers,
            json={"common_header_type": "minimal", "engine": "ReplacingMergeTree"},
        )
        assert patched.status_code == 200, patched.text
        _create_source(client, admin_headers, "alpha")
        _create_source(client, admin_headers, "beta")

        resp = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha"]})

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"updated": ["alpha"], "unchanged": []}
        alpha = _version(client, admin_headers, "alpha")
        assert alpha["header"] == {"type": "minimal", "version": "1.0.0"}
        assert alpha["schema"]["ttl_days"] == 90
        assert alpha["schema"]["engine"] == "ReplacingMergeTree"
        assert alpha["match"]["value"] == "alpha"
        beta = _version(client, admin_headers, "beta")
        assert beta.get("header") is None
        assert beta["schema"]["ttl_days"] is None
        assert beta["schema"]["engine"] != "ReplacingMergeTree"

    def test_a_source_already_on_the_defaults_is_unchanged(
        self, admin_headers, app, client, tmp_path
    ):
        _wire(app, tmp_path)
        _create_source(client, admin_headers, "alpha")
        first = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha"]})
        assert first.status_code == 200, first.text

        second = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha", "alpha"]})

        assert second.status_code == 200, second.text
        assert second.json() == {"updated": [], "unchanged": ["alpha"]}

    def test_a_missing_source_writes_nothing(self, admin_headers, app, client, tmp_path):
        _wire(app, tmp_path)
        _create_source(client, admin_headers, "alpha")

        resp = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha", "missing"]})

        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == "not_found"
        assert _version(client, admin_headers, "alpha")["header"] is None

    def test_an_empty_list_is_422(self, admin_headers, client):
        resp = client.post(APPLY, headers=admin_headers, json={"sources": []})

        assert resp.status_code == 422, resp.text

    def test_a_viewer_cannot_apply_them(self, client, viewer_headers):
        resp = client.post(APPLY, headers=viewer_headers, json={"sources": ["alpha"]})

        assert resp.status_code == 403, resp.text
