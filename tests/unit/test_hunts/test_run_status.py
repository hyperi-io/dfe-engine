#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_run_status.py
#  Purpose:      The run read models map CH rows to what the API reports
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""read_run_status turns one CH result into per-hunt run state.

The JOIN itself is proved against real ClickHouse in tests/integration. What is
pinned here is the reading of its rows, because ClickHouse fills a LEFT JOIN miss
with the type's DEFAULT rather than NULL. Every "never happened" in this model is
therefore a zero, and telling "wrote no rows" apart from "has not run" is a
decision this code makes rather than something the query says.
"""

from __future__ import annotations

from dfe_engine.hunts.run_status import clear_auto_disable, live_runner_count, read_run_status


class _Rows:
    """A ClickHouse client that returns one fixed result set."""

    def __init__(self, rows: list[list]) -> None:
        self._rows = rows
        self.parameters: dict | None = None

    def query(self, sql: str, parameters: dict | None = None):
        self.parameters = parameters
        self.sql = sql
        return type("Result", (), {"result_rows": self._rows})()


class _Recording(_Rows):
    """A client that also records what is inserted."""

    def __init__(self, rows: list[list]) -> None:
        super().__init__(rows)
        self.inserts: list[tuple] = []

    def insert(self, table: str, data: list[list], column_names: list[str], database: str):
        self.inserts.append((table, data, column_names, database))


def _row(
    hunt_id,
    wm=0,
    lease=0,
    overruns=0,
    aggressive=0,
    last_fire=0,
    rows=0,
    pending=0,
    disabled_at=None,
    disabled_reason="",
):
    return [
        hunt_id,
        wm,
        lease,
        overruns,
        aggressive,
        last_fire,
        rows,
        pending,
        disabled_at,
        disabled_reason,
    ]


def test_a_hunt_that_has_never_run_reports_nothing_rather_than_zero():
    status = read_run_status(_Rows([_row("fresh")]), "dfe", ["fresh"], now=1000)["fresh"]
    assert status.last_run is None
    assert status.last_run_rows is None
    assert status.running is False
    assert status.too_aggressive is False
    assert status.run_requested is False


def test_a_run_that_wrote_no_rows_is_not_a_hunt_that_never_ran():
    # Both come back as 0 from ClickHouse. The fire is what separates them.
    status = read_run_status(
        _Rows([_row("quiet", wm=900, last_fire=900, rows=0)]), "dfe", ["quiet"], now=1000
    )["quiet"]
    assert status.last_run == 900
    assert status.last_run_rows == 0


def test_rows_written_by_the_last_run_are_reported():
    status = read_run_status(
        _Rows([_row("busy", wm=900, last_fire=900, rows=42)]), "dfe", ["busy"], now=1000
    )["busy"]
    assert status.last_run_rows == 42


def test_running_is_a_lease_that_has_not_expired():
    live = read_run_status(_Rows([_row("h", lease=1500)]), "dfe", ["h"], now=1000)["h"]
    stale = read_run_status(_Rows([_row("h", lease=900)]), "dfe", ["h"], now=1000)["h"]
    assert live.running is True
    assert stale.running is False


def test_too_aggressive_and_overruns_come_through():
    status = read_run_status(
        _Rows([_row("late", overruns=3, aggressive=1)]), "dfe", ["late"], now=1000
    )["late"]
    assert status.too_aggressive is True
    assert status.overrun_count == 3


def test_a_queued_run_is_reported_as_requested():
    status = read_run_status(_Rows([_row("h", pending=1)]), "dfe", ["h"], now=1000)["h"]
    assert status.run_requested is True


def test_a_hunt_that_was_never_auto_disabled_reports_neither_field():
    status = read_run_status(_Rows([_row("h")]), "dfe", ["h"], now=1000)["h"]
    assert status.auto_disabled_at is None
    assert status.auto_disabled_reason == ""


def test_an_auto_disabled_hunt_reports_when_and_why():
    status = read_run_status(
        _Rows([_row("h", disabled_at=950, disabled_reason="resource_limit:241")]),
        "dfe",
        ["h"],
        now=1000,
    )["h"]
    assert status.auto_disabled_at == 950
    assert status.auto_disabled_reason == "resource_limit:241"


def test_a_null_reason_reads_as_empty():
    status = read_run_status(_Rows([_row("h", disabled_reason=None)]), "dfe", ["h"], now=1000)["h"]
    assert status.auto_disabled_reason == ""


def test_the_query_selects_the_auto_disable_columns_from_hunt_state():
    ch = _Rows([])
    read_run_status(ch, "dfe", ["h"], now=1000)
    # One argMax over the tuple: argMax skips a NULL argument, and a cleared stop is one.
    assert "argMax((auto_disabled_at, auto_disabled_reason), updated) AS d" in ch.sql
    assert "WHERE endsWith(hunt_id, {mark:String})" in ch.sql
    assert "toUnixTimestamp(tupleElement(d.disabled, 1))" in ch.sql


def test_clearing_an_auto_disable_writes_the_stop_marker_with_no_time_and_reads_nothing():
    ch = _Recording([[3, 1]])
    clear_auto_disable(ch, "dfe", "h")
    # No read: the counters live in the hunt's own row and are not touched.
    assert ch.parameters is None
    assert ch.inserts == [
        (
            "hunt_state",
            [["h#stopped", 0, 0, None, ""]],
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


def test_no_hunt_ids_asks_clickhouse_nothing():
    ch = _Rows([])
    assert read_run_status(ch, "dfe", [], now=1000) == {}
    assert ch.parameters is None


def test_the_hunt_ids_are_passed_as_a_bound_parameter():
    # Hunt names come from the config store, and they go in as a parameter rather
    # than interpolated into the SQL.
    ch = _Rows([])
    read_run_status(ch, "dfe", ["a", "b"], now=1000)
    assert ch.parameters == {"ids": ["a", "b"], "mark": "#stopped"}
    assert "{ids:Array(String)}" in ch.sql


def test_the_live_runner_count_is_whatever_clickhouse_counted():
    # The freshness comparison is ClickHouse's. This side only reads the one number.
    assert live_runner_count(_Rows([[2]]), "dfe", now=1000) == 2


def test_no_heartbeat_rows_is_no_live_runner():
    assert live_runner_count(_Rows([]), "dfe", now=1000) == 0


def test_the_live_runner_clock_is_passed_as_a_bound_parameter():
    ch = _Rows([[0]])
    live_runner_count(ch, "dfe", now=1000)
    assert ch.parameters == {"now": 1000}
    assert "{now:Int64}" in ch.sql
    # Each runner is judged against the cadence it recorded, not a shared setting.
    assert "2 * p" in ch.sql
