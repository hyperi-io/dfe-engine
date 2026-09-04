#  Project:      dfe-engine
#  File:         hunt_runner/run_status.py
#  Purpose:      Per-hunt run status for the API, in one ClickHouse read
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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


def _query(database: str) -> str:
    """The per-hunt join over watermark, lease, state and run."""
    return (
        "SELECT h.hunt_id, coalesce(w.wm, 0), coalesce(l.lu, 0), "
        "coalesce(s.overruns, 0), coalesce(s.aggressive, 0), "
        "coalesce(r.last_fire, 0), coalesce(r.rows_written, 0), coalesce(q.pending, 0) "
        "FROM (SELECT arrayJoin({ids:Array(String)}) AS hunt_id) h "
        "LEFT JOIN (SELECT hunt_id, argMax(watermark, updated) AS wm "
        f"FROM `{database}`.hunt_watermark GROUP BY hunt_id) w USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, argMax(lease_until, claimed) AS lu "
        f"FROM `{database}`.hunt_lease GROUP BY hunt_id) l USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, argMax(overrun_count, updated) AS overruns, "
        "argMax(too_aggressive, updated) AS aggressive "
        f"FROM `{database}`.hunt_state GROUP BY hunt_id) s USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, max(fire) AS last_fire, "
        "argMax(rows_written, fire) AS rows_written FROM ("
        "SELECT hunt_id, fire, argMax(status, updated) AS status, "
        "argMax(rows_written, updated) AS rows_written "
        f"FROM `{database}`.hunt_run GROUP BY hunt_id, fire"
        ") WHERE status = 'completed' GROUP BY hunt_id) r USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, 1 AS pending FROM ("
        "SELECT hunt_id, fire, argMax(status, updated) AS status "
        f"FROM `{database}`.hunt_run GROUP BY hunt_id, fire"
        ") WHERE status = 'requested' GROUP BY hunt_id) q USING (hunt_id)"
    )


def read_run_status(ch: Any, database: str, hunt_ids: list[str], now: int) -> dict[str, RunStatus]:
    """Run status for each named hunt. Missing ids simply do not come back.

    Args:
        ch: a ClickHouse client exposing ``query``.
        database: the data database the coordination tables live in.
        hunt_ids: the hunts to report on (the page's own list).
        now: epoch seconds, used to decide whether a lease is still live.
    """
    if not hunt_ids:
        return {}
    rows = ch.query(_query(database), parameters={"ids": list(hunt_ids)}).result_rows
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
        )
    return status
