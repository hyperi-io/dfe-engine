#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_run_visibility.py
#  Purpose:      Hunt list rows carry run state, and run-now queues a fire
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""With no ClickHouse to answer, the hunts list still renders with empty run state
and a queued run reports 503 only for a connection failure, 500 for anything else.
"""

from __future__ import annotations

import time

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

_HUNT = {
    "name": "visible_hunt",
    "display_name": "Visible Hunt",
    "cron": "*/5 * * * *",
    "global_target_table_name": "dfe.detection",
    "global_source_table_name": "dfe.main",
    "customers": ["org_a"],
    "rules": ["some_rule"],
}


@pytest.fixture
def created_hunt(client, admin_headers):
    assert client.post("/api/v1/hunts", json=_HUNT, headers=admin_headers).status_code == 201
    try:
        yield _HUNT["name"]
    finally:
        client.delete(f"/api/v1/hunts/{_HUNT['name']}", headers=admin_headers)


def test_a_hunt_row_carries_its_run_state(client, admin_headers, created_hunt):
    resp = client.get("/api/v1/hunts", headers=admin_headers)
    assert resp.status_code == 200
    row = next(item for item in resp.json()["items"] if item["name"] == created_hunt)
    assert row["last_run"] is None
    assert row["running"] is False
    assert row["last_run_rows"] is None
    assert row["too_aggressive"] is False
    assert row["run_requested"] is False


def test_next_due_is_derived_from_the_cron(client, admin_headers, created_hunt):
    now = int(time.time())
    row = next(
        item
        for item in client.get("/api/v1/hunts", headers=admin_headers).json()["items"]
        if item["name"] == created_hunt
    )
    # */5 -> a 300s interval, so the next fire is ahead of now and inside one interval.
    assert row["next_due"] > now
    assert row["next_due"] - now <= 300


def test_queueing_a_run_says_so_rather_than_claiming_it_ran(client, admin_headers, created_hunt):
    resp = client.post(f"/api/v1/hunts/{created_hunt}/run", headers=admin_headers)
    # No ClickHouse here, so the queue write cannot land. It must say that, not 202.
    assert resp.status_code == 503
    assert resp.json()["code"] == "coordination_unavailable"


def test_queueing_a_run_needs_the_execute_grant(client, viewer_headers, created_hunt):
    resp = client.post(f"/api/v1/hunts/{created_hunt}/run", headers=viewer_headers)
    assert resp.status_code == 403


class _RefusingDriver:
    """Driver-shaped client that refuses the connection, like a ClickHouse that is down."""

    def insert(self, *args, **kwargs):
        raise ConnectionError("[Errno 111] Connection refused")

    def close(self) -> None:
        return None


class _BrokenDriver:
    """Driver-shaped client with a programming fault - reachable, but the call is wrong."""

    def insert(self, *args, **kwargs):
        raise TypeError("insert() got an unexpected keyword argument 'column_names'")

    def close(self) -> None:
        return None


@pytest.fixture
def install_driver():
    """Put a driver-shaped client under the engine's ClickHouse manager for one test."""
    ClickHouseManager.reset_instance()
    manager = ClickHouseManager.get_instance()

    def _install(driver) -> None:
        manager._client = driver

    yield _install
    ClickHouseManager.reset_instance()


def test_a_refused_connection_is_the_only_thing_that_says_unavailable(
    client, admin_headers, created_hunt, install_driver
):
    install_driver(_RefusingDriver())
    resp = client.post(f"/api/v1/hunts/{created_hunt}/run", headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["code"] == "coordination_unavailable"


def test_a_programming_fault_is_a_500_not_an_outage_claim(
    client, admin_headers, created_hunt, install_driver
):
    install_driver(_BrokenDriver())
    resp = client.post(f"/api/v1/hunts/{created_hunt}/run", headers=admin_headers)
    # ClickHouse answered the socket; saying it is unreachable would be a lie.
    assert resp.status_code == 500
    assert resp.json()["code"] == "queue_failed"
