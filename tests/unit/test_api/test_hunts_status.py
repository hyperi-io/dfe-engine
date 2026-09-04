#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_status.py
#  Purpose:      /hunts/status reports runner liveness from the heartbeat table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``running`` on the status endpoint means a runner beat recently, not a lease.

Hunts execute in a separate service, and a lease exists only while a hunt is
mid-execution, so a healthy idle runner used to read as not running - which is
what the dfe-ui header badge shows. The heartbeat is what an idle runner leaves
behind, and these pin both ends of reading it: a beat counted, and a ClickHouse
that cannot answer degrading to "no runners" rather than a 500.

The heartbeat rows themselves are written against a real ClickHouse in
tests/integration/test_hunt_runner_reload.py.
"""

from __future__ import annotations

import pytest

from dfe_engine.api.v1 import hunts


class _Count:
    """A ClickHouse client that answers every query with one fixed count."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.parameters: dict | None = None

    def query(self, sql: str, parameters: dict | None = None):
        self.parameters = parameters
        return type("Result", (), {"result_rows": [[self._count]]})()


@pytest.fixture
def counted(monkeypatch):
    """Point the status endpoint at a client returning a chosen runner count."""

    def _install(count: int) -> _Count:
        client = _Count(count)
        monkeypatch.setattr(hunts, "_data_database_client", lambda: (client, "dfe"))
        return client

    return _install


def test_a_runner_that_beat_recently_reads_as_running(client, admin_headers, counted):
    ch = counted(1)
    body = client.get("/api/v1/hunts/status", headers=admin_headers).json()
    assert body["running"] is True
    assert body["runners"] == 1
    # The freshness cut-off is the API's clock, bound as a parameter.
    assert set(ch.parameters or {}) == {"now"}


def test_no_runner_beating_reads_as_not_running(client, admin_headers, counted):
    counted(0)
    body = client.get("/api/v1/hunts/status", headers=admin_headers).json()
    assert body["running"] is False
    assert body["runners"] == 0


def test_clickhouse_being_down_is_no_runners_rather_than_an_error(
    client, admin_headers, monkeypatch
):
    def _boom():
        raise RuntimeError("clickhouse is unreachable")

    monkeypatch.setattr(hunts, "_data_database_client", _boom)
    resp = client.get("/api/v1/hunts/status", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["running"] is False
    assert resp.json()["runners"] == 0
