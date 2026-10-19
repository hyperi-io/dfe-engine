#  Project:      dfe-engine
#  File:         tests/unit/test_usage/test_usage_reader.py
#  Purpose:      Which usage table and binds a read takes, and how its rows come back
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The reader's choices before ClickHouse sees a query.

What each query answers against real tables is tests/integration/test_usage_ch.py.
Here the double stands in for the ClickHouse driver: it records what the reader
asks and hands back rows.
"""

from datetime import date, datetime

import pytest
from dfe_schemas.usage import (
    ALL_ORGS,
    CORES_DEPLOYED,
    EVENTS_PER_SECOND,
    INGESTED_BYTES,
    METRICS,
)

from dfe_engine.usage import (
    UsagePoint,
    UsageReader,
    UsageUnavailableError,
    usage_metric,
    usage_metric_names,
)

D1, D2 = date(2026, 10, 1), date(2026, 10, 2)


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_rows = rows


class _ClickHouse:
    """Records every query and answers each with the same rows."""

    def __init__(self, rows: list[tuple] | None = None, *, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, dict, dict]] = []

    def query(self, sql: str, parameters=None, settings=None) -> _Result:
        self.calls.append((sql, dict(parameters or {}), dict(settings or {})))
        if self.fail:
            raise RuntimeError("clickhouse is down")
        return _Result(self.rows)


def _read(metric: str, scopes: list[str], rows: list[tuple] | None = None):
    ch = _ClickHouse(rows)
    points = UsageReader(ch, "dfe").daily(
        usage_metric(metric), scopes=scopes, first_day=D1, last_day=D2
    )
    return points, ch


def test_the_names_are_the_ones_dfe_schemas_declares():
    assert usage_metric_names() == [metric.name for metric in METRICS]
    assert usage_metric(INGESTED_BYTES).per_org is True


def test_an_unknown_metric_is_a_key_error():
    with pytest.raises(KeyError):
        usage_metric("bytes_out")


def test_the_deployment_reads_the_daily_rows_of_the_empty_scope():
    _, ch = _read(CORES_DEPLOYED, [ALL_ORGS])

    [(sql, params, settings)] = ch.calls
    assert "FROM dfe.usage_daily FINAL" in sql
    assert params == {
        "metric": CORES_DEPLOYED,
        "scopes": [ALL_ORGS],
        "first_day": D1,
        "last_day": D2,
    }
    assert settings == {"max_execution_time": 10}


def test_a_summed_metric_over_several_scopes_sums_their_daily_rows():
    _, ch = _read(INGESTED_BYTES, ["t-b", "t-a", "t-a"])

    [(sql, params, _)] = ch.calls
    assert "FROM dfe.usage_daily FINAL" in sql
    assert "sum(value)" in sql
    assert params["scopes"] == ["t-a", "t-b"]


def test_a_peak_metric_over_one_scope_reads_its_daily_rows():
    _, ch = _read(EVENTS_PER_SECOND, ["t-a"])

    [(sql, _, _)] = ch.calls
    assert "FROM dfe.usage_daily FINAL" in sql


def test_a_peak_metric_over_several_scopes_reads_the_minutes():
    """The sum of two orgs' daily peaks overstates the day: they peak at different minutes."""
    _, ch = _read(EVENTS_PER_SECOND, ["t-a", "t-b"])

    [(sql, params, _)] = ch.calls
    assert "FROM dfe.usage_minute FINAL" in sql
    assert "usage_daily" not in sql
    assert params["scopes"] == ["t-a", "t-b"]


def test_rows_come_back_as_points_in_their_order():
    points, _ = _read(INGESTED_BYTES, [ALL_ORGS], rows=[(D1, 10), (datetime(2026, 10, 2), 2.5)])

    assert points == [UsagePoint(day=D1, value=10.0), UsagePoint(day=D2, value=2.5)]


def test_an_iso_date_is_read_as_a_date():
    points, _ = _read(INGESTED_BYTES, [ALL_ORGS], rows=[("2026-10-01", 1)])

    assert points == [UsagePoint(day=D1, value=1.0)]


def test_no_rows_is_no_points_never_a_zero():
    points, _ = _read(INGESTED_BYTES, [ALL_ORGS], rows=[])

    assert points == []


def test_a_read_held_to_no_scope_is_refused_before_any_query():
    ch = _ClickHouse()

    with pytest.raises(ValueError, match="no scope"):
        UsageReader(ch, "dfe").daily(
            usage_metric(INGESTED_BYTES), scopes=[], first_day=D1, last_day=D2
        )
    assert ch.calls == []


def test_a_range_ending_before_it_starts_is_refused():
    with pytest.raises(ValueError, match="ends before it"):
        UsageReader(_ClickHouse(), "dfe").daily(
            usage_metric(INGESTED_BYTES), scopes=[ALL_ORGS], first_day=D2, last_day=D1
        )


def test_a_failed_read_raises_rather_than_reading_as_no_usage():
    with pytest.raises(UsageUnavailableError, match="daily"):
        UsageReader(_ClickHouse(fail=True), "dfe").daily(
            usage_metric(INGESTED_BYTES), scopes=[ALL_ORGS], first_day=D1, last_day=D2
        )


@pytest.mark.parametrize("database", ["dfe; DROP TABLE x", "dfe.other", "", "dfe`"])
def test_an_unsafe_database_name_is_refused(database):
    with pytest.raises(ValueError, match="unsafe database"):
        UsageReader(_ClickHouse(), database)


def test_the_database_lands_in_every_query():
    _, ch = _read(EVENTS_PER_SECOND, ["t-a", "t-b"])
    _, daily = _read(INGESTED_BYTES, [ALL_ORGS])

    assert "__DB__" not in ch.calls[0][0]
    assert "__DB__" not in daily.calls[0][0]
