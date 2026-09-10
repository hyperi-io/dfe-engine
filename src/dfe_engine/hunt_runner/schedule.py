#  Project:      dfe-engine
#  File:         hunt_runner/schedule.py
#  Purpose:      Deterministic-due schedule materialisation + the KEDA scaler query
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Scale-from-zero for the pull-based hunt runner: the due decision, in pure SQL.

KEDA can only wake the FIRST worker if "is any hunt due?" is answerable with NO
worker running - i.e. as a ClickHouse query KEDA runs itself. The per-hunt schedule
(interval + the stable phase offset) otherwise lives only in a running worker's
memory, so it is MATERIALISED into a small CH table (``hunt_schedule``) at
config-deploy time (an Argo post-sync hook, or the engine on a hunt-config change).

The scaler query then counts due-and-unclaimed hunts using the SAME arithmetic the
worker uses (``spread.latest_fire``): for a hunt with interval I and stable offset
P, boundary = now // I * I, fire = boundary + P if now >= boundary + P else
boundary + P - I (the latest fire at or before now), and the hunt is due iff

    watermark < fire       # that fire is not already completed
    AND no active lease    # it is not currently running

That EXACT parity with the worker's own skip conditions (runner.tick) is what makes
scale-to-zero safe: KEDA wakes a worker precisely when a worker would find work, and
scales to zero precisely when a worker would idle. There is no timer to race - KEDA
gates on the real backlog signal. See docs/data-plane/hunt-runner-scaling.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import HuntSpec
from .spread import phase_offset

_COLUMNS = ["hunt_id", "interval_seconds", "phase_offset", "enabled"]


def ensure_schedule_schema(ch: Any, database: str) -> None:
    """Create the materialised schedule table if absent (idempotent).

    ReplacingMergeTree keyed by hunt_id: one logical row per hunt after merge, with
    ``enabled`` as a soft tombstone so a removed hunt stops waking KEDA without a
    delete. ``updated`` is the replacing version (latest write wins per hunt_id).
    """
    ch.command(
        f"CREATE TABLE IF NOT EXISTS `{database}`.hunt_schedule ("
        "hunt_id String, interval_seconds Int64, phase_offset Int64, "
        "enabled UInt8 DEFAULT 1, updated DateTime64(3) DEFAULT now64(3)) "
        "ENGINE = ReplacingMergeTree(updated) ORDER BY hunt_id"
    )


def _enabled_ids(ch: Any, database: str) -> set[str]:
    """hunt_ids currently materialised as enabled (for tombstone diffing)."""
    rows = ch.query(
        "SELECT hunt_id FROM (SELECT hunt_id, argMax(enabled, updated) AS e "
        f"FROM `{database}`.hunt_schedule GROUP BY hunt_id) WHERE e = 1"
    ).result_rows
    return {r[0] for r in rows}


def publish_schedule(
    ch: Any,
    database: str,
    specs: Mapping[str, HuntSpec],
    *,
    fraction: float = 0.8,
) -> int:
    """Materialise the deterministic schedule KEDA scales on. Returns live count.

    Writes one enabled=1 row per live hunt carrying the SAME phase offset the worker
    computes (spread.phase_offset - single source of truth, so KEDA's due view can
    never drift from the worker's), and tombstones (enabled=0) any hunt that was
    enabled before but is absent now, so a deleted hunt no longer wakes KEDA.

    Idempotent: safe to call on every hunt-config change (the Argo post-sync hook).
    """
    ensure_schedule_schema(ch, database)
    rows: list[list[Any]] = []
    live_ids: set[str] = set()
    for spec in specs.values():
        rows.append(
            [
                spec.hunt_id,
                spec.interval_seconds,
                phase_offset(spec.hunt_id, spec.interval_seconds, fraction),
                1,
            ]
        )
        live_ids.add(spec.hunt_id)
    for gone in _enabled_ids(ch, database) - live_ids:
        rows.append([gone, 0, 0, 0])  # tombstone: enabled=0, filtered before any intDiv
    if rows:
        ch.insert("hunt_schedule", rows, column_names=_COLUMNS, database=database)
    return len(live_ids)


def due_query(database: str) -> str:
    """The KEDA ClickHouse-scaler query: number of due-and-unclaimed hunts.

    Pure-SQL parity with spread.latest_fire + runner.tick's skip conditions. The
    enabled=1 filter is applied in the inner subquery so a tombstone (interval=0)
    can never reach the intDiv (no divide-by-zero). KEDA scales workers on this
    count (targetValue = hunts-per-worker) and to zero when it returns 0.
    """
    current = "(intDiv(toInt64(now()), s.interval_seconds) * s.interval_seconds + s.phase_offset)"
    fire = f"if(toInt64(now()) >= {current}, {current}, {current} - s.interval_seconds)"
    return (
        "SELECT count() AS due FROM ("
        "SELECT hunt_id, interval_seconds, phase_offset FROM ("
        "SELECT hunt_id, argMax(interval_seconds, updated) AS interval_seconds, "
        "argMax(phase_offset, updated) AS phase_offset, argMax(enabled, updated) AS enabled "
        f"FROM `{database}`.hunt_schedule GROUP BY hunt_id) WHERE enabled = 1) s "
        "LEFT JOIN (SELECT hunt_id, argMax(watermark, updated) AS wm "
        f"FROM `{database}`.hunt_watermark GROUP BY hunt_id) w USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, argMax(lease_until, claimed) AS lu "
        f"FROM `{database}`.hunt_lease GROUP BY hunt_id) l USING (hunt_id) "
        f"WHERE coalesce(w.wm, 0) < {fire} "
        "AND coalesce(l.lu, 0) <= toInt64(now())"
    )


def due_count(ch: Any, database: str) -> int:
    """Run due_query and return the backlog count (KEDA's metric; also for the UI)."""
    rows = ch.query(due_query(database)).result_rows
    return int(rows[0][0]) if rows else 0
