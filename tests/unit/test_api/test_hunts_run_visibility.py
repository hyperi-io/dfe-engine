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

pytestmark = pytest.mark.usefixtures("customer_orgs")

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


def test_next_due_follows_the_configured_phase_spread(client, app, admin_headers, created_hunt):
    from dfe_engine.hunts.schedule import next_due, phase_offset

    assert phase_offset(created_hunt, 300, 0.05) != phase_offset(created_hunt, 300)
    app.state.settings.hunts.phase_spread_fraction = 0.05
    before = int(time.time())
    row = next(
        item
        for item in client.get("/api/v1/hunts", headers=admin_headers).json()["items"]
        if item["name"] == created_hunt
    )
    after = int(time.time())
    assert row["next_due"] in {next_due(created_hunt, 300, now, 0.05) for now in (before, after)}


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
    # ClickHouse answered the socket. Saying it is unreachable would be a lie.
    assert resp.status_code == 500
    assert resp.json()["code"] == "queue_failed"


class _CoordinationClient:
    """A ClickHouse client whose run-status row says whether the hunt is auto-disabled."""

    def __init__(self, hunt_id: str, disabled_at: int | None, reason: str = "") -> None:
        self._row = [hunt_id, 0, 0, 3, 1, 0, 0, 0, disabled_at, reason]
        self.inserts: list[tuple] = []

    def query(self, sql: str, parameters: dict | None = None):
        if sql.startswith("SELECT h.hunt_id"):
            return type("Result", (), {"result_rows": [self._row]})()
        return type("Result", (), {"result_rows": [[3, 1]]})()

    def insert(self, table: str, data: list[list], column_names: list[str], database: str):
        self.inserts.append((table, data, column_names, database))


@pytest.fixture
def coordination(monkeypatch, created_hunt):
    """Install a coordination client for the created hunt, disabled or not."""
    from dfe_engine.api.v1 import hunts

    def _install(disabled_at: int | None, reason: str = "") -> _CoordinationClient:
        client = _CoordinationClient(created_hunt, disabled_at, reason)
        monkeypatch.setattr(hunts, "_data_database_client", lambda: (client, "dfe"))
        return client

    return _install


def _update_body(**changes) -> dict:
    body = {k: v for k, v in _HUNT.items() if k != "name"}
    return {**body, **changes}


def test_a_hunt_row_carries_enabled_and_the_auto_disable(
    client, admin_headers, created_hunt, coordination
):
    coordination(1700000000, "resource_limit:241")
    row = next(
        item
        for item in client.get("/api/v1/hunts", headers=admin_headers).json()["items"]
        if item["name"] == created_hunt
    )
    assert row["enabled"] is True
    assert row["auto_disabled_at"] == 1700000000
    assert row["auto_disabled_reason"] == "resource_limit:241"


def test_a_hunt_saved_disabled_lists_as_disabled_and_keeps_the_field_in_its_detail(
    client, admin_headers, created_hunt
):
    resp = client.put(
        f"/api/v1/hunts/{created_hunt}", json=_update_body(enabled=False), headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    row = next(
        item
        for item in client.get("/api/v1/hunts", headers=admin_headers).json()["items"]
        if item["name"] == created_hunt
    )
    assert row["enabled"] is False
    assert (
        client.get(f"/api/v1/hunts/{created_hunt}", headers=admin_headers).json()["enabled"]
        is False
    )


def test_enabling_an_auto_disabled_hunt_clears_the_auto_disable(
    client, admin_headers, created_hunt, coordination
):
    ch = coordination(1700000000, "resource_limit:241")
    resp = client.put(
        f"/api/v1/hunts/{created_hunt}", json=_update_body(enabled=True), headers=admin_headers
    )
    assert resp.status_code == 200
    assert ch.inserts == [
        (
            "hunt_state",
            [[f"{created_hunt}#stopped", 0, 0, None, ""]],
            [
                "hunt_id",
                "overrun_count",
                "too_aggressive",
                "auto_disabled_at",
                "auto_disabled_reason",
            ],
            "dfe",
        )
    ]


@pytest.mark.parametrize(
    ("changes", "disabled_at"),
    [
        ({"enabled": False}, 1700000000),
        ({"enabled": True}, None),
        ({"cron": "*/7 * * * *"}, 1700000000),
    ],
    ids=["saved-disabled", "never-auto-disabled", "an-edit-that-omits-enabled"],
)
def test_a_save_that_does_not_re_enable_an_auto_disabled_hunt_writes_no_state(
    client, admin_headers, created_hunt, coordination, changes, disabled_at
):
    ch = coordination(disabled_at, "resource_limit:241" if disabled_at else "")
    resp = client.put(
        f"/api/v1/hunts/{created_hunt}", json=_update_body(**changes), headers=admin_headers
    )
    assert resp.status_code == 200
    assert ch.inserts == []


def test_a_clear_clickhouse_did_not_take_is_a_503_not_a_quiet_200(
    client, admin_headers, created_hunt, coordination
):
    ch = coordination(1700000000, "resource_limit:241")

    def _refuse(*args, **kwargs):
        raise ConnectionError("[Errno 111] Connection refused")

    ch.insert = _refuse
    resp = client.put(
        f"/api/v1/hunts/{created_hunt}", json=_update_body(enabled=True), headers=admin_headers
    )
    assert resp.status_code == 503
    assert resp.json()["code"] == "auto_disable_not_cleared"


def test_a_save_routed_to_a_review_branch_lifts_no_stop(created_hunt, coordination):
    from dfe_engine.api.v1.hunts import _clear_auto_disable
    from dfe_engine.gitcrud.routing import WriteOutcome

    ch = coordination(1700000000, "resource_limit:241")
    routed = WriteOutcome(changed=True, review_required=True, branch="dfe/hunt/x/1")
    _clear_auto_disable(created_hunt, enabled=True, outcome=routed)
    assert ch.inserts == []
    _clear_auto_disable(created_hunt, enabled=True, outcome=WriteOutcome(changed=True))
    assert len(ch.inserts) == 1


def test_an_edit_that_omits_enabled_keeps_a_hunt_disabled(client, admin_headers, created_hunt):
    off = client.put(
        f"/api/v1/hunts/{created_hunt}", json=_update_body(enabled=False), headers=admin_headers
    )
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    edit = client.put(
        f"/api/v1/hunts/{created_hunt}",
        json=_update_body(cron="*/7 * * * *"),
        headers=admin_headers,
    )
    assert edit.status_code == 200
    assert edit.json()["enabled"] is False
    assert (
        client.get(f"/api/v1/hunts/{created_hunt}", headers=admin_headers).json()["enabled"]
        is False
    )


def test_the_detail_carries_the_runners_stop_so_a_direct_link_shows_it(
    client, admin_headers, created_hunt, coordination
):
    coordination(1700000000, "resource_limit:241")
    detail = client.get(f"/api/v1/hunts/{created_hunt}", headers=admin_headers).json()
    assert detail["enabled"] is True
    assert detail["auto_disabled_at"] == 1700000000
    assert detail["auto_disabled_reason"] == "resource_limit:241"


def test_a_direct_query_hunt_with_no_rules_is_a_detail_not_a_500():
    from dfe_engine.api.v1.hunts import HuntDetailResponse

    detail = HuntDetailResponse.from_stored_config(
        "stop_limit",
        {"cron": "* * * * *", "display_name": "Stop Limit", "query": "SELECT 1 WHERE {window}"},
    )
    assert detail.rules == []
    assert detail.query == "SELECT 1 WHERE {window}"
    assert detail.enabled is True
