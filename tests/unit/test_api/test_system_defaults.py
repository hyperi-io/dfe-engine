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
No source here is deployed, so apply and drift never need a table; what they do
to a deployed one is proven in tests/integration/test_system_defaults_apply_ch.py.
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


@pytest.fixture
def unreachable_clickhouse(api_settings):
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


class TestPatchDefaults:
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
        body = resp.json()
        assert body["updated"] == ["alpha"]
        assert body["unchanged"] == []
        assert [entry["source"] for entry in body["live"]] == ["alpha"]
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
        assert second.json()["updated"] == []
        assert second.json()["unchanged"] == ["alpha"]
        assert [entry["source"] for entry in second.json()["live"]] == ["alpha"]

    def test_a_source_never_deployed_is_pinned_without_reaching_clickhouse(
        self, unreachable_clickhouse, admin_headers, app, client, tmp_path
    ):
        _wire(app, tmp_path)
        _create_source(client, admin_headers, "alpha")

        resp = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha"]})

        assert resp.status_code == 200, resp.text
        assert resp.json()["updated"] == ["alpha"]
        assert resp.json()["live"] == [
            {
                "source": "alpha",
                "status": "not_deployed",
                "table": None,
                "ttl": None,
                "columns_added": [],
                "not_applied": [],
                "reason": "never deployed; its table is created with these values",
            }
        ]

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


DRIFT = "/api/v1/system/defaults/drift"


class TestDefaultDrift:
    def test_only_sources_storing_a_different_value_are_listed(
        self, admin_headers, app, client, tmp_path
    ):
        _wire(app, tmp_path)
        _create_source(client, admin_headers, "alpha")
        pinned = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha"]})
        assert pinned.status_code == 200, pinned.text
        patched = client.patch(
            URL,
            headers=admin_headers,
            json={"common_header_type": "minimal", "engine": "ReplacingMergeTree"},
        )
        assert patched.status_code == 200, patched.text
        _create_source(client, admin_headers, "beta")

        resp = client.get(DRIFT, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        by_name = {item["source"]: item for item in resp.json()["items"]}
        assert "main" not in by_name
        assert "beta" not in by_name
        alpha = by_name["alpha"]
        assert alpha["core"] is False
        assert alpha["drifted"] == ["common_header_type", "engine"]
        assert alpha["common_header_type"] == {
            "stored": "timeseries",
            "default": "minimal",
            "live": None,
        }
        assert alpha["engine"] == {
            "stored": "MergeTree",
            "default": "ReplacingMergeTree",
            "live": None,
        }
        assert alpha["ttl_days"]["stored"] == 90
        assert alpha["ttl_days"]["default"] == 90
        assert "ttl_days" not in alpha["drifted"]

    def test_a_header_named_by_its_registry_path_is_not_drift(
        self, unreachable_clickhouse, admin_headers, app, client, tmp_path
    ):
        _wire(app, tmp_path)
        resp = client.post(
            "/api/v1/sources",
            headers=admin_headers,
            json={
                "source": "alpha",
                "match": {"field": "tags.collector.type", "value": "alpha"},
                "header": {"type": "common-header/timeseries", "version": "1.0.0"},
                "schema": {"ttl_days": 90, "engine": "MergeTree"},
            },
        )
        assert resp.status_code == 201, resp.text

        drift = client.get(DRIFT, headers=admin_headers)

        assert drift.status_code == 200, drift.text
        assert drift.json()["items"] == []

    def test_applying_the_defaults_clears_the_drift(self, admin_headers, app, client, tmp_path):
        _wire(app, tmp_path)
        _create_source(client, admin_headers, "alpha")
        client.post(APPLY, headers=admin_headers, json={"sources": ["alpha"]})
        client.patch(
            URL,
            headers=admin_headers,
            json={"common_header_type": "minimal", "engine": "ReplacingMergeTree"},
        )

        applied = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha"]})
        assert applied.status_code == 200, applied.text

        resp = client.get(DRIFT, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert "alpha" not in {item["source"] for item in resp.json()["items"]}

    def test_a_viewer_cannot_read_drift(self, client, viewer_headers):
        resp = client.get(DRIFT, headers=viewer_headers)

        assert resp.status_code == 403, resp.text

    def test_search_and_page_the_drifted_sources(self, admin_headers, app, client, tmp_path):
        _wire(app, tmp_path)
        _create_source(client, admin_headers, "alpha")
        _create_source(client, admin_headers, "zeta")
        pinned = client.post(APPLY, headers=admin_headers, json={"sources": ["alpha", "zeta"]})
        assert pinned.status_code == 200, pinned.text
        client.patch(
            URL,
            headers=admin_headers,
            json={"common_header_type": "minimal", "engine": "ReplacingMergeTree"},
        )

        named = client.get(DRIFT, headers=admin_headers, params={"search": "alpha"})
        assert named.status_code == 200, named.text
        assert [item["source"] for item in named.json()["items"]] == ["alpha"]
        assert named.json()["total"] == 1

        landing = client.get(DRIFT, headers=admin_headers, params={"search": "main"})
        assert landing.json()["items"] == []
        assert landing.json()["total"] == 0

        by_engine = client.get(DRIFT, headers=admin_headers, params={"search": "MergeTree"})
        assert [item["source"] for item in by_engine.json()["items"]] == ["alpha", "zeta"]

        page = client.get(DRIFT, headers=admin_headers, params={"page": 1, "per_page": 1})
        assert page.status_code == 200, page.text
        body = page.json()
        assert len(body["items"]) == 1
        assert body["total"] == 2
        assert body["page"] == 1
        assert body["per_page"] == 1
        assert body["next_page"] == 2
        assert body["items"][0]["source"] != "main"
