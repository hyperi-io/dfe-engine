#  Project:      dfe-engine
#  File:         hunts/checkpoint.py
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

A resumed rule hunt also re-reads the overlap before its watermark, so a row that
committed or replicated after the previous fire read past it is still matched. The
statement skips a row already detected, so the overlap never writes it twice.

The watermark is epoch seconds and the common-header column is DateTime64(3), so
the predicate casts its bounds rather than comparing a bare integer. ClickHouse
reads an integer bound against a DateTime64 column as epoch seconds either way,
so the cast also leaves a hunt over an Int64 timestamp column selecting the same
rows.
"""

# The always-present common-header column - the FIXED watermark field.
TIMESTAMP_FIELD = "_timestamp_load"


def window(
    last_watermark: int | None,
    scheduled_start: int,
    interval_seconds: int,
    first_fire: int | None = None,
) -> tuple[int, int]:
    """Return (start, end) epoch bounds for the incremental query.

    start = last committed watermark, or an interval before the hunt's first fire
    while there is none. ``first_fire`` is the fire the hunt first attempted, kept by
    the worker, so a first fire that never completes does not move the start to a
    later fire and skip the rows between. end = scheduled_start. The query is
    `_timestamp_load >= start AND < end`.
    """
    end = scheduled_start
    if last_watermark is not None:
        return last_watermark, end
    anchor = first_fire if first_fire is not None else scheduled_start
    return anchor - interval_seconds, end


def read_from(start: int, last_watermark: int | None, overlap_seconds: int) -> int:
    """Return where a deduplicating statement starts reading: the overlap before *start*.

    A first run has no earlier window to overlap, so it keeps its interval lookback.
    """
    if last_watermark is None:
        return start
    return start - overlap_seconds


def predicate(start: int, end: int, field: str = TIMESTAMP_FIELD) -> str:
    """The SQL predicate for the window, epoch-second bounds cast to DateTime64(3)."""
    return f"({field} >= toDateTime64({start}, 3) AND {field} < toDateTime64({end}, 3))"
