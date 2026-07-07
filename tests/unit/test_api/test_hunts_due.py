#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_due.py
#  Purpose:      GET /system/hunts-due - the KEDA metrics-api scaling metric
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The hunt-runner autoscaling metric endpoint KEDA's metrics-api scaler polls -
so KEDA never needs a ClickHouse wire port. Returns {"due": N}; fails safe to 0."""

from __future__ import annotations

import clickhouse_connect

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager


class _FakeResult:
    result_rows = [(5,)]


class _FakeCH:
    def query(self, sql, *a, **k):
        return _FakeResult()

    def command(self, *a, **k):
        return None

    def close(self):
        pass


class TestHuntsDue:
    def test_returns_due_count(self, client, admin_headers, monkeypatch):
        ClickHouseManager.reset_instance()
        monkeypatch.setattr(clickhouse_connect, "get_client", lambda **kw: _FakeCH())
        resp = client.get("/api/v1/system/hunts-due", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"due": 5}
        ClickHouseManager.reset_instance()

    def test_fails_safe_to_zero_on_ch_error(self, client, admin_headers, monkeypatch):
        # A scaling metric must NEVER spuriously scale up (or block scale-to-zero)
        # because the count call blipped - a CH error reports due=0, not a 500.
        ClickHouseManager.reset_instance()

        def _boom(**kw):
            raise RuntimeError("ClickHouse unreachable")

        monkeypatch.setattr(clickhouse_connect, "get_client", _boom)
        resp = client.get("/api/v1/system/hunts-due", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"due": 0}
        ClickHouseManager.reset_instance()

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/system/hunts-due")
        assert resp.status_code == 401
