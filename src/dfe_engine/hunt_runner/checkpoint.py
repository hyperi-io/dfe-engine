#  Project:      dfe-engine
#  File:         hunt_runner/checkpoint.py
#  Purpose:      Incremental checkpoint window on _timestamp_load (crash-safe resume)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Compute a hunt's incremental query window on `_timestamp_load`.

Each run queries only the NEW window: `_timestamp_load >= start AND < end`. The
watermark (last committed end) advances ONLY after results commit, so a crashed
pod re-runs from the last committed watermark - no gap, no loss. First run looks
back by the interval.

The watermark is epoch seconds and the common-header column is DateTime64(3), so
the predicate casts its bounds rather than comparing a bare integer. ClickHouse
reads an integer bound against a DateTime64 column as epoch seconds either way,
so the cast also leaves a hunt over an Int64 timestamp column selecting the same
rows.
"""

from __future__ import annotations

# The always-present common-header column - the FIXED watermark field.
TIMESTAMP_FIELD = "_timestamp_load"


def window(
    last_watermark: int | None,
    scheduled_start: int,
    interval_seconds: int,
) -> tuple[int, int]:
    """Return (start, end) epoch bounds for the incremental query.

    start = last committed watermark, or (scheduled_start - interval) on first run.
    end   = scheduled_start. The query is `_timestamp_load >= start AND < end`.
    """
    end = scheduled_start
    start = last_watermark if last_watermark is not None else scheduled_start - interval_seconds
    return start, end


def predicate(start: int, end: int, field: str = TIMESTAMP_FIELD) -> str:
    """The SQL predicate for the window, epoch-second bounds cast to DateTime64(3)."""
    return f"({field} >= toDateTime64({start}, 3) AND {field} < toDateTime64({end}, 3))"
