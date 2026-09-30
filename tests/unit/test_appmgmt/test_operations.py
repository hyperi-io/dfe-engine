#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_operations.py
#  Purpose:      Per-source signals read off the loader's per-table counter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The arithmetic between a counter series and a number a console can show.

A monotonic counter says nothing on its own: the rate is a difference over
elapsed time, and "when did records last arrive" is the last time the total
MOVED, not the last time the loader reported. Both go wrong quietly, so both
are asserted here rather than only through the route.
"""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt.operations import (
    GAUGE_METRICS,
    HTTP_P95,
    HTTP_REQUESTS,
    HTTP_SERVER_ERRORS,
    RESOURCE_METRICS,
    SCALING_PRESSURE,
    MetricsUnavailableError,
    OperationalReader,
    http_readings,
)

LOADERS = ["dfe-loader-main"]


class FakeClickHouse:
    def __init__(self, rows=None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, dict, dict]] = []

    def execute(self, sql: str, parameters=None, settings=None):
        self.calls.append((sql, dict(parameters or {}), dict(settings or {})))
        if self.fail:
            raise RuntimeError("clickhouse is down")
        return self.rows


def _reader(rows=None, fail: bool = False) -> tuple[OperationalReader, FakeClickHouse]:
    ch = FakeClickHouse(rows, fail)
    return OperationalReader(ch, "dfe"), ch


def test_the_rate_is_the_delta_over_the_elapsed_time():
    reader, _ = _reader([(0.0, 100.0), (60.0, 220.0), (120.0, 340.0)])

    signals = reader.source_signals("auth", LOADERS)

    assert signals.records_per_min == pytest.approx(120.0)
    assert signals.last_seen_epoch == 120.0


def test_one_sample_cannot_be_differenced():
    reader, _ = _reader([(0.0, 100.0)])

    assert reader.source_signals("auth", LOADERS).records_per_min is None


def test_a_reset_counter_reports_nothing_rather_than_a_negative_rate():
    # A pod restart zeroes its counter; the window has to roll past the restart
    # before the number means anything again.
    reader, _ = _reader([(0.0, 900.0), (60.0, 10.0)])

    assert reader.source_signals("auth", LOADERS).records_per_min is None


def test_a_flat_counter_reports_no_arrival():
    reader, _ = _reader([(0.0, 500.0), (60.0, 500.0), (120.0, 500.0)])

    signals = reader.source_signals("auth", LOADERS)

    assert signals.records_per_min == 0.0
    assert signals.last_seen_epoch is None


def test_the_last_arrival_is_the_last_increase_not_the_last_report():
    reader, _ = _reader([(0.0, 10.0), (60.0, 40.0), (120.0, 40.0), (180.0, 40.0)])

    assert reader.source_signals("auth", LOADERS).last_seen_epoch == 60.0


def test_no_loader_asks_the_database_nothing():
    reader, ch = _reader([(0.0, 1.0), (60.0, 2.0)])

    signals = reader.source_signals("auth", [])

    assert ch.calls == []
    assert signals.records_per_min is None
    assert signals.last_seen_epoch is None


def test_the_query_is_bound_and_time_bounded():
    reader, ch = _reader([(0.0, 1.0), (60.0, 2.0)])

    reader.source_signals("auth", LOADERS)

    sql, params, settings = ch.calls[0]
    assert "dfe.otel_metrics_sum" in sql
    assert params == {"services": LOADERS, "table": "auth", "window_seconds": 300}
    assert settings["max_execution_time"] == 10


def test_a_database_failure_is_raised_rather_than_reported_as_zero():
    reader, _ = _reader(fail=True)

    with pytest.raises(MetricsUnavailableError):
        reader.source_signals("auth", LOADERS)


@pytest.mark.parametrize("bad", ["auth; DROP TABLE x", "dfe.auth", "auth table"])
def test_an_unsafe_table_name_is_refused(bad):
    reader, _ = _reader()

    with pytest.raises(ValueError):
        reader.source_signals(bad, LOADERS)


def test_pressure_is_asked_for_under_the_canonical_bare_name():
    # The apps emit it under four different prefixes, so the reader names the one
    # scalo registers and matches the rest by rule.
    assert SCALING_PRESSURE == "scaling_pressure"
    assert SCALING_PRESSURE in GAUGE_METRICS
    assert SCALING_PRESSURE in RESOURCE_METRICS


def test_the_gauge_query_matches_any_prefix_and_reports_one_name():
    reader, ch = _reader([])

    reader.metrics("dfe-fetcher")

    sql = ch.calls[0][0]
    assert "endsWith(MetricName, '_scaling_pressure')" in sql
    assert "'scaling_pressure', MetricName" in sql  # normalised back to one key
    assert "GROUP BY MetricName, cityHash64(Attributes)" in sql


def test_the_series_query_buckets_pressure_under_that_same_one_name():
    reader, ch = _reader([])

    reader.resource_series("dfe-fetcher", window_seconds=600, bucket_seconds=60)

    sql = ch.calls[0][0]
    assert "endsWith(MetricName, '_scaling_pressure')" in sql
    assert "GROUP BY bucket, metric" in sql


# ── liveness: max() over no rows is the epoch, not NULL ─────────


class PerQueryClickHouse:
    """Answers each query with rows chosen by a fragment of its SQL."""

    def __init__(self, by_fragment: dict[str, list[tuple]]) -> None:
        self.by_fragment = by_fragment

    def execute(self, sql: str, parameters=None, settings=None):
        for fragment, rows in self.by_fragment.items():
            if fragment in sql:
                return rows
        return []


def test_an_instance_that_never_emitted_is_not_reporting():
    # ClickHouse answers max() over an empty match with one row carrying the
    # type's default, so the epoch plus a zero count is what "never emitted" is.
    ch = PerQueryClickHouse({"AS last_seen": [(0.0, 0)]})

    status = OperationalReader(ch, "dfe").status("dfe-transform-elastic-nosuchinstance")

    assert status.reporting is False
    assert status.last_seen_epoch is None


def test_an_instance_with_telemetry_is_reporting():
    ch = PerQueryClickHouse({"AS last_seen": [(1757000000.0, 12)]})

    status = OperationalReader(ch, "dfe").status("dfe-loader")

    assert status.reporting is True
    assert status.last_seen_epoch == 1757000000.0


def test_uptime_reads_the_start_time_every_dfe_app_emits():
    # A scalo-py service reports start_time_seconds and no process_start_time_seconds.
    ch = PerQueryClickHouse(
        {
            "AS last_seen": [(1757000600.0, 3)],
            "SELECT metric, max(value)": [("start_time_seconds", 1757000000.0)],
        }
    )

    status = OperationalReader(ch, "dfe").status("dfe-engine")

    assert status.started_epoch == 1757000000.0


# ── HTTP readings from the request-duration histogram ───────────

BOUNDS = [0.1, 0.5, 1.0]


def _series(*, first, last, server_error=False, span=240, started_in_window=False):
    """One series row as http_request_histogram returns it."""
    return (server_error, BOUNDS, first, last, sum(first), sum(last), span, started_in_window)


def test_rates_and_p95_come_from_the_change_across_the_window():
    rows = [_series(first=[10, 0, 0, 0], last=[90, 15, 4, 1])]

    rates, gauges = http_readings(rows=rows)

    assert rates == {HTTP_REQUESTS: 100 / 240, HTTP_SERVER_ERRORS: 0.0}
    # 80 of 100 in the first bucket, so the 95th sits at the top of the second.
    assert gauges == {HTTP_P95: pytest.approx(0.5)}


def test_server_errors_count_only_5xx_series():
    rows = [
        _series(first=[0, 0, 0, 0], last=[30, 0, 0, 0]),
        _series(first=[0, 0, 0, 0], last=[6, 0, 0, 0], server_error=True),
    ]

    rates, _ = http_readings(rows=rows)

    assert rates[HTTP_REQUESTS] == 36 / 240
    assert rates[HTTP_SERVER_ERRORS] == 6 / 240


def test_a_series_that_went_backwards_is_left_out():
    rows = [
        _series(first=[0, 0, 0, 0], last=[12, 0, 0, 0]),
        _series(first=[50, 0, 0, 0], last=[5, 0, 0, 0]),
    ]

    rates, _ = http_readings(rows=rows)

    assert rates[HTTP_REQUESTS] == 12 / 240


def test_requests_above_the_top_bound_report_that_bound():
    rows = [_series(first=[0, 0, 0, 0], last=[0, 0, 0, 20])]

    _, gauges = http_readings(rows=rows)

    assert gauges == {HTTP_P95: 1.0}


def test_no_histogram_gives_no_http_readings_and_no_traffic_gives_no_p95():
    assert http_readings(rows=[]) == ({}, {})

    rates, gauges = http_readings(rows=[_series(first=[5, 0, 0, 0], last=[5, 0, 0, 0])])

    assert rates == {HTTP_REQUESTS: 0.0, HTTP_SERVER_ERRORS: 0.0}
    assert gauges == {}


def test_metrics_carries_the_http_readings():
    ch = PerQueryClickHouse(
        {"http.server.request.duration": [_series(first=[0, 0, 0, 0], last=[24, 0, 0, 0])]}
    )

    readings = OperationalReader(ch, "dfe").metrics("dfe-engine")

    assert readings.rates[HTTP_REQUESTS] == 24 / 240
    assert readings.gauges[HTTP_P95] == pytest.approx(0.095)


def test_a_series_born_in_the_window_counts_its_first_export():
    # A first 5xx on a route is its own new series; its only export must still count.
    rows = [
        _series(first=[0, 0, 0, 0], last=[40, 0, 0, 0]),
        _series(first=[1, 0, 0, 0], last=[1, 0, 0, 0], server_error=True, started_in_window=True),
    ]

    rates, _ = http_readings(rows=rows)

    assert rates[HTTP_SERVER_ERRORS] == 1 / 240
    assert rates[HTTP_REQUESTS] == 41 / 240
