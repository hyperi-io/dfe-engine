#  Project:      dfe-engine
#  File:         hunts/run_status.py
#  Purpose:      Per-hunt run status and runner liveness for the API
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the Hunts page needs to say whether a hunt ran, read in one query.

Everything here already exists in the coordination tables: the watermark is the
last successful run, the lease is "running right now", hunt_state carries the
too-aggressive flag, and hunt_run carries what the last run wrote. The gap this
closes is a read model, not new plumbing -- the page was left querying ClickHouse
itself because the API would not tell it.

ONE query for every hunt on the page, driven by the ids the caller already has:
a row per hunt joined to each table's current value. ClickHouse fills a LEFT JOIN
miss with the type's default rather than NULL, so a hunt that has never run reads
as zeros, and ``last_run``/``last_fire`` of 0 is what "never" looks like. That
matters for the row count: 0 rows written by a run that DID happen is a real and
different answer from a hunt that has not run at all.

``live_runner_count`` is the other half the page needs, and it reads a different
table: whether a runner exists at all, from the per-runner heartbeat rather than
from a lease that only exists while a hunt is mid-execution.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.hunts.ch_coordinator import STOP_MARK


@dataclass(frozen=True, slots=True)
class RunStatus:
    """One hunt's run state, as the API reports it."""

    hunt_id: str
    last_run: int | None = None
    """Watermark: the end of the last window that committed. None = never run."""
    running: bool = False
    """A live lease on this hunt right now."""
    last_run_rows: int | None = None
    """Rows the last completed run wrote. None = no run has been recorded."""
    too_aggressive: bool = False
    overrun_count: int = 0
    run_requested: bool = False
    """An operator asked for a run the runner has not served yet."""
    auto_disabled_at: int | None = None
    """Epoch seconds the runner stopped the hunt after repeated resource-limit failures."""
    auto_disabled_reason: str = ""
    """The ClickHouse error category and code that tripped the auto-disable."""


def _query(database: str) -> str:
    """The per-hunt join over watermark, lease, state and run."""
    db = quote_identifier(database)
    return (
        "SELECT h.hunt_id, coalesce(w.wm, 0), coalesce(l.lu, 0), "  # noqa: S608 - quoted configured database, ids bound
        "coalesce(s.overruns, 0), coalesce(s.aggressive, 0), "
        "coalesce(r.last_fire, 0), coalesce(r.rows_written, 0), coalesce(q.pending, 0), "
        "toUnixTimestamp(tupleElement(d.disabled, 1)), coalesce(tupleElement(d.disabled, 2), '') "
        "FROM (SELECT arrayJoin({ids:Array(String)}) AS hunt_id) h "
        "LEFT JOIN (SELECT hunt_id, argMax(watermark, updated) AS wm "
        f"FROM {db}.hunt_watermark GROUP BY hunt_id) w USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, argMax(lease_until, claimed) AS lu "
        f"FROM {db}.hunt_lease GROUP BY hunt_id) l USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, argMax(overrun_count, updated) AS overruns, "
        "argMax(too_aggressive, updated) AS aggressive "
        f"FROM {db}.hunt_state GROUP BY hunt_id) s USING (hunt_id) "
        # The stop is its own marker row, read as one tuple: argMax skips a NULL
        # argument, and a cleared stop is one.
        f"LEFT JOIN (SELECT substring(k, 1, length(k) - {len(STOP_MARK)}) AS hunt_id, "
        "d AS disabled FROM (SELECT hunt_id AS k, "
        "argMax((auto_disabled_at, auto_disabled_reason), updated) AS d "
        f"FROM {db}.hunt_state WHERE endsWith(hunt_id, {{mark:String}}) GROUP BY hunt_id)) "
        "d USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, max(fire) AS last_fire, "
        "argMax(rows_written, fire) AS rows_written FROM ("
        "SELECT hunt_id, fire, argMax(status, updated) AS status, "
        "argMax(rows_written, updated) AS rows_written "
        f"FROM {db}.hunt_run GROUP BY hunt_id, fire"
        ") WHERE status = 'completed' GROUP BY hunt_id) r USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, 1 AS pending FROM ("
        "SELECT hunt_id, fire, argMax(status, updated) AS status "
        f"FROM {db}.hunt_run GROUP BY hunt_id, fire"
        ") WHERE status = 'requested' GROUP BY hunt_id) q USING (hunt_id)"
    )


def live_runner_count(ch: Any, database: str, now: int) -> int:
    """How many hunt runners have ticked within their own last two polls.

    Two polls is the tolerance: one poll late is jitter, two is a dead runner. Each
    runner is judged against the cadence it beat with rather than a shared setting,
    so a runner started on a slower poll is not called dead for keeping to it.

    Args:
        ch: a ClickHouse client exposing ``query``.
        database: the data database the coordination tables live in.
        now: epoch seconds to judge the beats against.
    """
    rows = ch.query(
        "SELECT countIf(s > {now:Int64} - 2 * p) FROM ("  # noqa: S608 - quoted configured database, values bound
        "SELECT runner_id, argMax(seen, updated) AS s, argMax(poll_seconds, updated) AS p "
        f"FROM {quote_identifier(database)}.hunt_runner_heartbeat GROUP BY runner_id)",
        parameters={"now": now},
    ).result_rows
    return int(rows[0][0]) if rows else 0


def read_run_status(ch: Any, database: str, hunt_ids: list[str], now: int) -> dict[str, RunStatus]:
    """Run status for each named hunt. Missing ids do not come back.

    Args:
        ch: a ClickHouse client exposing ``query``.
        database: the data database the coordination tables live in.
        hunt_ids: the hunts to report on (the page's own list).
        now: epoch seconds, used to decide whether a lease is still live.
    """
    if not hunt_ids:
        return {}
    rows = ch.query(
        _query(database), parameters={"ids": list(hunt_ids), "mark": STOP_MARK}
    ).result_rows
    status: dict[str, RunStatus] = {}
    for row in rows:
        hunt_id = str(row[0])
        watermark = int(row[1])
        last_fire = int(row[5])
        status[hunt_id] = RunStatus(
            hunt_id=hunt_id,
            last_run=watermark or None,
            running=int(row[2]) > now,
            last_run_rows=int(row[6]) if last_fire else None,
            too_aggressive=bool(row[4]),
            overrun_count=int(row[3]),
            run_requested=bool(row[7]),
            auto_disabled_at=int(row[8]) if row[8] else None,
            auto_disabled_reason=str(row[9] or ""),
        )
    return status


def clear_auto_disable(ch: Any, database: str, hunt_id: str) -> None:
    """Write the hunt's stop marker row with a NULL time, which lifts the runner's stop.

    The stop is its own row, keyed ``<hunt_id>#stopped``, so the overrun counters are
    untouched and there is no read to race the runner. The row carries its own
    ``updated``: ClickHouse drops an insert block identical to a recent one on a
    replicated table, and a clear left to the column default is the same block every
    time, so the second clear of a hunt in its lifetime would never land.

    Args:
        ch: a ClickHouse client exposing ``insert``.
        database: the data database the coordination tables live in.
        hunt_id: the hunt to re-enable.
    """
    ch.insert(
        "hunt_state",
        [[f"{hunt_id}{STOP_MARK}", 0, 0, None, "", datetime.now(UTC)]],
        column_names=[
            "hunt_id",
            "overrun_count",
            "too_aggressive",
            "auto_disabled_at",
            "auto_disabled_reason",
            "updated",
        ],
        database=database,
    )


@dataclass(frozen=True, slots=True)
class HuntExecutionRow:
    """One finished hunt fire, newest facts after the replacing merge."""

    fire: int
    status: str
    rows_returned: int
    error: str
    execution_time_ms: float
    cost_window_minutes: int
    cost_estimate: str


def _execution_sql(database: str) -> str:
    """Fires that ran, newest first.

    A ``requested`` row with an error is a run-now whose attempt failed and that
    stays queued for the next one, so it lists as failed.
    """
    db = quote_identifier(database)
    return (
        "SELECT fire, if(status = 'requested', 'failed', status) AS shown, rows_written, "  # noqa: S608 - quoted database, bound hunt id
        "error, duration_ms, cost_window_minutes, cost_estimate FROM ("
        "SELECT fire, argMax(status, updated) AS status, "
        "argMax(rows_written, updated) AS rows_written, "
        "argMax(error, updated) AS error, "
        "argMax(duration_ms, updated) AS duration_ms, "
        "argMax(cost_window_minutes, updated) AS cost_window_minutes, "
        "argMax(cost_estimate, updated) AS cost_estimate "
        f"FROM {db}.hunt_run WHERE hunt_id = {{hunt:String}} GROUP BY fire"
        ") WHERE status != 'requested' OR error != '' ORDER BY fire DESC"
    )


def read_hunt_executions(
    ch: Any, database: str, hunt_id: str, *, limit: int, offset: int
) -> tuple[list[HuntExecutionRow], int]:
    """One page of a hunt's finished fires, and how many there are.

    A run-now that has not executed is not a fire here. The page is newest fire first.

    Args:
        ch: a ClickHouse client exposing ``query``.
        database: the data database the coordination tables live in.
        hunt_id: the hunt whose fires to list.
        limit: rows in this page.
        offset: finished fires to skip.
    """
    sql = _execution_sql(database)
    parameters = {"hunt": hunt_id}
    total_rows = ch.query(
        "SELECT count() FROM (" + sql + ") AS fires",  # noqa: S608 - the statement is built above
        parameters=parameters,
    ).result_rows
    total = int(total_rows[0][0]) if total_rows else 0
    rows = ch.query(
        sql + " LIMIT {limit:UInt32} OFFSET {offset:UInt32}",
        parameters={**parameters, "limit": limit, "offset": offset},
    ).result_rows
    executions = [
        HuntExecutionRow(
            fire=int(row[0]),
            status=str(row[1]),
            rows_returned=int(row[2]),
            error=str(row[3] or ""),
            execution_time_ms=float(row[4] or 0),
            cost_window_minutes=int(row[5] or 0),
            cost_estimate=str(row[6] or ""),
        )
        for row in rows
    ]
    return executions, total
