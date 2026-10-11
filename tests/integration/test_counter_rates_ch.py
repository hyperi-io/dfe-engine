#  Project:      dfe-engine
#  File:         tests/integration/test_counter_rates_ch.py
#  Purpose:      Live-CH proof the console's counter rates difference each pod on its own
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The console's counter readings against real ClickHouse.

Rows land in the real ``otel_metrics_sum`` table, rendered from the manifest the
schema phase applies, and ``OperationalReader`` answers over them with no mocks.
``TimeUnix`` has one-second precision and pods export on their own clocks, so the
fleet only ever lines up by accident: every test that matters here has its pods
exporting in different seconds. Each row is stamped relative to one server
``now()`` read at the start of the test, so the spacing between readings is exact.
"""

import pytest

from dfe_engine.appmgmt.operations import RECORDS_RECEIVED, OperationalReader
from dfe_engine.schema.plan import render_one

pytestmark = pytest.mark.integration

_SERVICE = "dfe-receiver"
_LOADER = "dfe-loader"
_LANDING = "loader_messages_by_table_total"
_TABLE = "auth"


@pytest.fixture
def otel_db(ch_client, clickhouse_test_database):
    """An isolated database carrying the three metrics tables the console reads."""
    db = clickhouse_test_database
    for object_id in (
        "data.otel_metrics_gauge",
        "data.otel_metrics_sum",
        "data.otel_metrics_histogram",
    ):
        for statement in render_one(object_id, data_database=db).statements:
            ch_client.command(statement)
    return db


@pytest.fixture
def base(ch_client) -> int:
    """The server's now() as an epoch second, read once for the whole test."""
    return int(ch_client.command("SELECT toUnixTimestamp(now())"))


def _write(
    ch_client,
    db: str,
    base: int,
    *points: tuple[int, float],
    pod: str,
    service: str = _SERVICE,
    metric: str = RECORDS_RECEIVED,
    attributes: dict[str, str] | None = None,
) -> None:
    """Write one cumulative reading per ``(age_seconds, value)``, stamped that long before ``base``."""
    attributes = attributes or {}
    resource = {"service.name": service, "service.instance.id": pod, "k8s.pod.name": pod}
    # clickhouse-connect binds a dict as JSON, which a Map parameter refuses to parse.
    for age, value in points:
        ch_client.command(
            f"INSERT INTO `{db}`.otel_metrics_sum "
            "(ServiceName, MetricName, Attributes, ResourceAttributes, TimeUnix, Value, "
            "AggregationTemporality, IsMonotonic) "
            "SELECT {service:String}, {metric:String}, "
            "mapFromArrays({attr_keys:Array(String)}, {attr_values:Array(String)}), "
            "mapFromArrays({res_keys:Array(String)}, {res_values:Array(String)}), "
            "toDateTime({base:UInt32} - {age:UInt32}), {value:Float64}, 2, true",
            parameters={
                "service": service,
                "metric": metric,
                "attr_keys": list(attributes),
                "attr_values": list(attributes.values()),
                "res_keys": list(resource),
                "res_values": list(resource.values()),
                "base": base,
                "age": age,
                "value": value,
            },
        )


def _rate(manager_client, db: str) -> float:
    return OperationalReader(manager_client, db).metrics(_SERVICE).rates[RECORDS_RECEIVED]


def _landing(ch_client, db: str, base: int, *points: tuple[int, float], pod: str) -> None:
    _write(
        ch_client,
        db,
        base,
        *points,
        pod=pod,
        service=_LOADER,
        metric=_LANDING,
        attributes={"table": _TABLE},
    )


# -- counter_rates: the instance's per-second rates --------------------------


def test_two_pods_exporting_seven_seconds_apart_read_their_combined_rate(
    ch_client, manager_client, otel_db, base
):
    """Each pod adds 100 over 60s, so the fleet carries 200/60 = 3.33 a second.

    Summing the fleet per timestamp first subtracts pod a's counter from pod b's
    across the window and reads (5100 - 1000) / 67 = 61.19.
    """
    _write(ch_client, otel_db, base, (67, 1000), (37, 1050), (7, 1100), pod="dfe-receiver-a")
    _write(ch_client, otel_db, base, (60, 5000), (30, 5050), (0, 5100), pod="dfe-receiver-b")

    assert _rate(manager_client, otel_db) == pytest.approx(200 / 60, rel=1e-3)


def test_a_counter_reset_mid_window_counts_from_zero(ch_client, manager_client, otel_db, base):
    """1000 -> 1040, then a restart to 20 -> 60: 40 + 20 + 40 = 100 over 60s.

    Differencing the first reading against the last reads (60 - 1000) / 60 = -15.67.
    """
    _write(
        ch_client, otel_db, base, (60, 1000), (40, 1040), (20, 20), (0, 60), pod="dfe-receiver-a"
    )

    assert _rate(manager_client, otel_db) == pytest.approx(100 / 60, rel=1e-3)


def test_a_single_pod_reads_its_own_rate(ch_client, manager_client, otel_db, base):
    """100 over 60s from one pod, which every keying of the fleet agrees on."""
    _write(ch_client, otel_db, base, (60, 1000), (30, 1050), (0, 1100), pod="dfe-receiver-a")

    assert _rate(manager_client, otel_db) == pytest.approx(100 / 60, rel=1e-3)


def test_a_pod_that_appears_mid_window_adds_its_own_rate(ch_client, manager_client, otel_db, base):
    """Pod a carries 100/60 for the whole window; pod b starts 33s ago and adds 60/30.

    The fleet's rate now is 100/60 + 2 = 3.67. Summing per timestamp reads only pod
    a's first and last readings, 100 / 60 = 1.67, and misses pod b entirely.
    """
    _write(ch_client, otel_db, base, (60, 1000), (30, 1050), (0, 1100), pod="dfe-receiver-a")
    _write(ch_client, otel_db, base, (33, 20), (3, 80), pod="dfe-receiver-b")

    assert _rate(manager_client, otel_db) == pytest.approx(100 / 60 + 2, rel=1e-3)


def test_two_attribute_sets_on_one_pod_are_two_series(ch_client, manager_client, otel_db, base):
    """One pod counts 100 over 60s on one attribute set and 60 on another: 160 / 60."""
    _write(
        ch_client,
        otel_db,
        base,
        (60, 1000),
        (30, 1050),
        (0, 1100),
        pod="dfe-receiver-a",
        attributes={"transport": "http"},
    )
    _write(
        ch_client,
        otel_db,
        base,
        (60, 50),
        (30, 80),
        (0, 110),
        pod="dfe-receiver-a",
        attributes={"transport": "grpc"},
    )

    assert _rate(manager_client, otel_db) == pytest.approx(160 / 60, rel=1e-3)


# -- source_landing_counter: one table's records a minute --------------------


def test_two_loader_pods_exporting_apart_read_their_combined_records_a_minute(
    ch_client, manager_client, otel_db, base
):
    """Each loader pod lands 100 records over 60s: 200 a minute, last risen at ``base``.

    Fleet totals per timestamp read (5100 - 1000) / 67s = 3671.6 a minute.
    """
    _landing(ch_client, otel_db, base, (67, 1000), (37, 1050), (7, 1100), pod="dfe-loader-a")
    _landing(ch_client, otel_db, base, (60, 5000), (30, 5050), (0, 5100), pod="dfe-loader-b")

    signals = OperationalReader(manager_client, otel_db).source_signals(_TABLE, [_LOADER])

    assert signals.records_per_min == pytest.approx(200.0, rel=1e-3)
    assert signals.last_seen_epoch == float(base)


def test_flat_counters_on_two_loader_pods_report_no_arrival(
    ch_client, manager_client, otel_db, base
):
    """Neither pod's counter moves, so nothing arrived.

    Fleet totals per timestamp alternate 1000, 5000, 1000, ... and read a rise
    every time pod b's export follows pod a's.
    """
    _landing(ch_client, otel_db, base, (67, 1000), (37, 1000), (7, 1000), pod="dfe-loader-a")
    _landing(ch_client, otel_db, base, (60, 5000), (30, 5000), (0, 5000), pod="dfe-loader-b")

    signals = OperationalReader(manager_client, otel_db).source_signals(_TABLE, [_LOADER])

    assert signals.records_per_min == 0.0
    assert signals.last_seen_epoch is None


def test_a_restarted_loader_pod_counts_from_zero(ch_client, manager_client, otel_db, base):
    """1000 -> 1040, a restart to 20 -> 60: 100 records over 60s, 100 a minute.

    Differencing the first reading against the last went negative and reported nothing.
    """
    _landing(
        ch_client, otel_db, base, (60, 1000), (40, 1040), (20, 20), (0, 60), pod="dfe-loader-a"
    )

    signals = OperationalReader(manager_client, otel_db).source_signals(_TABLE, [_LOADER])

    assert signals.records_per_min == pytest.approx(100.0, rel=1e-3)
    assert signals.last_seen_epoch == float(base)
