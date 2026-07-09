#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sampler.py
#  Purpose:      API contract tests for the sampler router
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sampler API tests - unified submit/poll contract + RBAC.

Fast modes (recent/random) come back ``completed`` with the result inline; gated
modes (smart/anomaly) come back ``pending`` for polling. The ClickHouse client is
overridden with a fake so no real CH is needed.
"""

from __future__ import annotations

import pytest

from dfe_engine.api.deps import get_clickhouse_client


class _Result:
    def __init__(self, rows: list[str]) -> None:
        self.result_rows = [[r] for r in rows]


class FakeCH:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def query(self, sql, parameters=None, settings=None):
        return _Result(self._rows)


@pytest.fixture
def fake_ch(app):
    """Override the CH client dependency with a fake returning two rows."""
    app.dependency_overrides[get_clickhouse_client] = lambda: FakeCH(
        ['{"a": 1, "b": "x"}', '{"a": 2}']
    )
    yield
    app.dependency_overrides.pop(get_clickhouse_client, None)


def test_recent_sample_completes_inline(client, fake_ch, admin_headers):
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`", "limit": 5},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["task_id"]
    result = body["result"]
    assert result["count"] == 2
    assert result["rows"] == [{"a": 1, "b": "x"}, {"a": 2}]
    assert result["keys"] == ["a", "b"]


def test_poll_endpoint_returns_result(client, fake_ch, admin_headers):
    submit = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`"},
        headers=admin_headers,
    ).json()
    got = client.get(f"/api/v1/samples/{submit['task_id']}", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["status"] == "completed"
    assert got.json()["result"]["count"] == 2


def test_smart_mode_returns_pending_task(client, fake_ch, admin_headers):
    # Gated mode is async by default (no inline wait) -> a task to poll.
    r = client.post(
        "/api/v1/sample",
        json={"mode": "smart", "table": "`db`.`events`"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"]
    assert body["status"] in {"pending", "running", "failed"}


def test_missing_target_is_400(client, fake_ch, admin_headers):
    r = client.post("/api/v1/sample", json={"mode": "recent"}, headers=admin_headers)
    assert r.status_code == 400
    assert "bad_sample_request" in r.text


def test_viewer_has_sampler_read(client, fake_ch, viewer_headers):
    r = client.post(
        "/api/v1/sample",
        json={"mode": "recent", "table": "`db`.`events`"},
        headers=viewer_headers,
    )
    assert r.status_code == 200, r.text


def test_unauthenticated_is_rejected(client, fake_ch):
    r = client.post("/api/v1/sample", json={"mode": "recent", "table": "`db`.`events`"})
    assert r.status_code in (401, 403)


def test_unknown_sample_task_is_404(client, admin_headers):
    r = client.get("/api/v1/samples/does-not-exist", headers=admin_headers)
    assert r.status_code == 404
