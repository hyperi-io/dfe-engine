#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_checkpoint.py
#  Purpose:      Tests for the incremental checkpoint window + claim SQL shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pure tests: checkpoint window and safe horizon math (the coordinator is integration-tested)."""

import pytest

from dfe_engine.hunts.checkpoint import TIMESTAMP_FIELD, predicate, read_from, window
from dfe_engine.hunts.safe_horizon import SafeHorizon, safe_horizon_sql


def test_first_run_looks_back_by_interval():
    start, end = window(None, scheduled_start=1000, interval_seconds=600)
    assert end == 1000
    assert start == 400


def test_subsequent_run_resumes_from_watermark():
    start, end = window(950, scheduled_start=1000, interval_seconds=600)
    assert start == 950  # resume exactly where we left off
    assert end == 1000


def test_predicate_uses_the_common_header_load_column():
    # The landing tables' column is _timestamp_load. timestamp_load exists nowhere.
    assert TIMESTAMP_FIELD == "_timestamp_load"


def test_predicate_casts_its_bounds_to_the_columns_type():
    # The watermark is epoch seconds and the column is DateTime64(3), so a bare
    # integer bound compares a count of seconds against a timestamp.
    p = predicate(950, 1000)
    assert p == (
        "(_timestamp_load >= toDateTime64(950, 3) AND _timestamp_load < toDateTime64(1000, 3))"
    )


@pytest.mark.parametrize(
    ("last", "fire", "interval", "overlap", "want"),
    [
        pytest.param(None, 1000, 600, 300, (400, 400, 1000), id="first_run_keeps_its_lookback"),
        pytest.param(950, 1000, 600, 300, (950, 650, 1000), id="resumed_reads_the_overlap"),
        pytest.param(950, 1000, 600, 0, (950, 950, 1000), id="no_overlap"),
    ],
)
def test_the_read_starts_one_overlap_before_the_watermark(last, fire, interval, overlap, want):
    start, end = window(last, scheduled_start=fire, interval_seconds=interval)

    assert (start, read_from(start, last, overlap), end) == want


def test_consecutive_fires_leave_no_gap_between_their_windows():
    first_start, first_end = window(None, scheduled_start=1000, interval_seconds=60)
    second_start, second_end = window(first_end, scheduled_start=1060, interval_seconds=60)

    assert (first_start, first_end, second_start, second_end) == (940, 1000, 1000, 1060)
    # The second fire's overlap re-reads the first one's window rather than skipping past it.
    assert read_from(second_start, first_end, 120) == 880


@pytest.mark.parametrize(
    ("delay", "now", "fire", "behind"),
    [
        pytest.param(0, 1000, 1000, 0, id="quiet_table_never_behind"),
        pytest.param(0, 990, 1000, 0, id="quiet_table_ahead_of_the_server_clock"),
        pytest.param(0, 1000, 990, 0, id="quiet_table_past_fire"),
        pytest.param(30, 1100, 1000, 0, id="horizon_above_fire"),
        pytest.param(30, 1030, 1000, 10, id="horizon_below_fire"),
        pytest.param(93, 1000, 1000, 103, id="worst_measured_lag"),
    ],
)
def test_the_safe_horizon_trails_the_delay_by_the_commit_allowance(delay, now, fire, behind):
    reading = SafeHorizon(now=now, delay_seconds=delay, commit_allowance_seconds=10)

    assert reading.seconds_behind(fire) == behind


def test_the_safe_horizon_reads_every_table_once_as_quoted_literals():
    sql = safe_horizon_sql(
        [("acme", "aws-guardduty"), ("acme", "detection"), ("acme", "detection")]
    )

    assert sql.endswith(
        "WHERE (database, table) IN (('acme', 'aws-guardduty'), ('acme', 'detection'))"
    )


def test_the_safe_horizon_reads_the_detection_tables_delay_on_its_own():
    sql = safe_horizon_sql([("acme", "main"), ("acme", "detection")], [("acme", "detection")])

    assert "maxIf(absolute_delay, (database, table) IN (('acme', 'detection')))" in sql


def test_a_safe_horizon_over_no_table_is_refused():
    with pytest.raises(ValueError, match="no table"):
        safe_horizon_sql([])
