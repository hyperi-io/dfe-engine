#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_wrapper_writes.py
#  Purpose:      The engine's ClickHouse client can WRITE rows, not only read them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The wrapper handed out by ``get_clickhouse_client()`` supports row inserts.

``ClickHouseClientWrapper`` is the only ClickHouse client the engine hands out, and
it used to expose query/command/execute but no ``insert``. Every writer that was
built against clickhouse-connect - the hunt coordinator, so every run-now, lease,
watermark and heartbeat written from the API process - therefore died on
``AttributeError: 'ClickHouseClientWrapper' object has no attribute 'insert'``. The
read paths hid it: they only ever call ``query``.

These build the coordinator over the SAME object the API builds it over (the
wrapper, with a recording connect-shaped client under it) and drive real coordinator
methods, so the wiring is what is asserted, not a restatement of the wrapper.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from dfe_engine.clickhouse.attribution import tags_context
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.hunt_runner.ch_coordinator import ChCoordinator


class _RecordingConnectClient:
    """A clickhouse-connect-shaped client that records calls instead of connecting.

    Not a mock of engine code - it stands in for the external driver at the only
    boundary a unit test cannot cross, and every assertion reads its real record.
    """

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list, dict]] = []

    def insert(self, table, data, **kwargs):
        self.inserts.append((table, data, kwargs))
        return None


@pytest.fixture
def wrapper_over_recorder():
    """The engine's wrapper with a recording driver client underneath it."""
    manager = ClickHouseManager()
    recorder = _RecordingConnectClient()
    # Stand in for the driver client so nothing connects; the wrapper resolves it live.
    manager._client = cast("Any", recorder)
    return manager.get_clickhouse_client(), recorder


def test_the_api_client_can_insert_rows(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder
    client.insert("hunt_run", [["h", 1, "requested", 0]], column_names=["a"], database="dfe")
    table, data, kwargs = recorder.inserts[0]
    assert table == "hunt_run"
    assert data == [["h", 1, "requested", 0]]
    assert kwargs["column_names"] == ["a"]
    assert kwargs["database"] == "dfe"


def test_a_coordinator_over_the_api_client_queues_a_run(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder
    ChCoordinator(client, database="dfe").request_run("visible_hunt", 1_700_000_000)
    table, data, kwargs = recorder.inserts[0]
    assert table == "hunt_run"
    assert data == [["visible_hunt", 1_700_000_000, "requested", 0]]
    assert kwargs["column_names"] == ["hunt_id", "fire", "status", "rows_written"]
    assert kwargs["database"] == "dfe"


def test_a_coordinator_over_the_api_client_records_a_completed_run(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder
    ChCoordinator(client, database="dfe").record_run("visible_hunt", 42, 7)
    table, data, _kwargs = recorder.inserts[0]
    assert table == "hunt_run"
    assert data == [["visible_hunt", 42, "completed", 7]]


def test_an_insert_carries_the_attribution_tag(wrapper_over_recorder):
    client, recorder = wrapper_over_recorder
    with tags_context(tenant_id="acme", feature="hunts"):
        client.insert("hunt_run", [["h", 1, "requested", 0]], database="dfe")
    _table, _data, kwargs = recorder.inserts[0]
    assert json.loads(kwargs["settings"]["log_comment"])["tenant_id"] == "acme"
