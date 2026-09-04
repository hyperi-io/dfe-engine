#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_checkpoint.py
#  Purpose:      Tests for the incremental checkpoint window + claim SQL shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pure tests: checkpoint window math (the coordinator is integration-tested)."""

from __future__ import annotations

from dfe_engine.hunt_runner import TIMESTAMP_FIELD, predicate, window


def test_first_run_looks_back_by_interval():
    start, end = window(None, scheduled_start=1000, interval_seconds=600)
    assert end == 1000
    assert start == 400


def test_subsequent_run_resumes_from_watermark():
    start, end = window(950, scheduled_start=1000, interval_seconds=600)
    assert start == 950  # resume exactly where we left off
    assert end == 1000


def test_predicate_uses_the_common_header_load_column():
    # The landing tables' column is _timestamp_load; timestamp_load exists nowhere.
    assert TIMESTAMP_FIELD == "_timestamp_load"


def test_predicate_casts_its_bounds_to_the_columns_type():
    # The watermark is epoch seconds and the column is DateTime64(3), so a bare
    # integer bound compares a count of seconds against a timestamp.
    p = predicate(950, 1000)
    assert p == (
        "(_timestamp_load >= toDateTime64(950, 3) AND _timestamp_load < toDateTime64(1000, 3))"
    )
