#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_clickhouse_cloud.py
#  Purpose:      Tests for the /api/v1/system/clickhouse-cloud endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CH Cloud lifecycle endpoints - unconfigured (no network) + configured (mocked)."""

from __future__ import annotations


class TestClickhouseCloudEndpoints:
    def test_status_unconfigured_reports_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/system/clickhouse-cloud", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["configured"] is False

    def test_start_unconfigured_returns_503(self, client, admin_headers):
        resp = client.post("/api/v1/system/clickhouse-cloud/start", headers=admin_headers)
        assert resp.status_code == 503

    def test_stop_unconfigured_returns_503(self, client, admin_headers):
        resp = client.post("/api/v1/system/clickhouse-cloud/stop", headers=admin_headers)
        assert resp.status_code == 503

    def test_status_requires_auth(self, client):
        resp = client.get("/api/v1/system/clickhouse-cloud")
        assert resp.status_code == 401

    def test_status_configured_reports_state(self, client, app, admin_headers, monkeypatch):
        """A configured cloud + a mocked CloudService surfaces the live state."""
        from dfe_engine.api.deps import get_app_settings
        from dfe_engine.clickhouse.cloud import CloudServiceStatus
        from dfe_engine.settings import DFESettings

        settings = DFESettings()
        settings.clickhouse.cloud.api_key_id = "k"
        settings.clickhouse.cloud.api_key_secret = "s"

        class FakeCloud:
            def __init__(self, cfg):
                self._cfg = cfg

            def status(self) -> CloudServiceStatus:
                return CloudServiceStatus(id="svc-1", name="dfe", state="running")

        monkeypatch.setattr("dfe_engine.clickhouse.cloud.CloudService", FakeCloud)
        app.dependency_overrides[get_app_settings] = lambda: settings
        try:
            resp = client.get("/api/v1/system/clickhouse-cloud", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["configured"] is True
            assert body["state"] == "running"
            assert body["is_running"] is True
        finally:
            app.dependency_overrides.pop(get_app_settings, None)
