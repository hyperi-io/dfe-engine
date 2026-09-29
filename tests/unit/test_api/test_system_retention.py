#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_retention.py
#  Purpose:      GET/PUT /api/v1/system/retention short of ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Who may read and set the default TTL, what a PUT accepts, and where the value is read.

Every path here is decided before ClickHouse is reached. An admin's PUT applying
the new TTL to live tables is proven against a real server in
``tests/integration/test_system_retention_ch.py``.
"""

import pytest

from dfe_engine.api.v1.system import RetentionUpdate
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.retention import MAX_DEFAULT_TTL_DAYS, set_stored
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import ClickHouseResilienceSettings

URL = "/api/v1/system/retention"


def _wire(app, tmp_path) -> GitCrud:
    """A local-only deploy repo behind the running app, as gitops on gives it one."""
    gc = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = gc
    return gc


class TestGetRetention:
    def test_reports_the_deployment_default_without_gitops(self, admin_headers, client):
        resp = client.get(URL, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "default_ttl_days": 90,
            "stored": None,
            "origin": "deployment",
            "deployment_default": 90,
            "editable": False,
        }

    def test_reports_a_stored_override(self, admin_headers, app, client, tmp_path):
        set_stored(_wire(app, tmp_path), 30, "admin")

        resp = client.get(URL, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "default_ttl_days": 30,
            "stored": 30,
            "origin": "override",
            "deployment_default": 90,
            "editable": True,
        }

    def test_a_viewer_cannot_read_it(self, client, viewer_headers):
        assert client.get(URL, headers=viewer_headers).status_code == 403


class TestPutRetentionRefusals:
    @pytest.mark.parametrize("who", ["viewer_headers", "operator_headers"])
    def test_a_non_admin_is_refused_and_nothing_is_committed(
        self, app, client, request, tmp_path, who
    ):
        gc = _wire(app, tmp_path)

        resp = client.put(URL, headers=request.getfixturevalue(who), json={"default_ttl_days": 30})

        assert resp.status_code == 403, resp.text
        assert gc.head_revision() is None

    @pytest.mark.parametrize(
        "body",
        [
            {"default_ttl_days": -1},
            {"default_ttl_days": MAX_DEFAULT_TTL_DAYS + 1},
            {"default_ttl_days": "30"},
            {"default_ttl_days": 30.5},
            {"default_ttl_days": True},
            {},
        ],
        ids=["negative", "over-100-years", "string", "fraction", "boolean", "missing"],
    )
    def test_an_invalid_value_is_422_and_nothing_is_committed(
        self, admin_headers, app, body, client, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = client.put(URL, headers=admin_headers, json=body)

        assert resp.status_code == 422, resp.text
        assert gc.head_revision() is None

    def test_without_gitops_there_is_nowhere_to_store_it(self, admin_headers, client):
        resp = client.put(URL, headers=admin_headers, json={"default_ttl_days": 30})

        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "not_configured"


class TestPutRetentionWhenClickHouseRefuses:
    @pytest.fixture
    def unreachable_clickhouse(self, api_settings):
        """Point the app at a port nothing listens on, with no retry to wait through."""
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

    def test_the_override_is_kept_and_the_failure_named(
        self, unreachable_clickhouse, admin_headers, app, client, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = client.put(URL, headers=admin_headers, json={"default_ttl_days": 30})

        assert resp.status_code == 502, resp.text
        assert resp.json()["code"] == "reconcile_failed"
        assert gc.get("gov_settings", "retention")["default_ttl_days"] == 30
        assert client.get(URL, headers=admin_headers).json()["origin"] == "override"


class TestRetentionUpdateBody:
    @pytest.mark.parametrize("days", [0, 1, MAX_DEFAULT_TTL_DAYS, None])
    def test_the_bounds_and_a_clear_are_accepted(self, days):
        assert RetentionUpdate.model_validate({"default_ttl_days": days}).default_ttl_days == days


class TestEveryReaderFollowsTheOverride:
    def test_the_settings_summary_reports_the_override(self, admin_headers, app, client, tmp_path):
        set_stored(_wire(app, tmp_path), 30, "admin")

        resp = client.get("/api/v1/system/settings", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["clickhouse_default_ttl_days"] == 30

    def test_setup_status_reports_the_override(self, app, client, tmp_path):
        set_stored(_wire(app, tmp_path), 30, "admin")

        resp = client.get("/api/v1/auth/setup-status")

        assert resp.status_code == 200, resp.text
        assert resp.json()["default_ttl_days"] == 30
