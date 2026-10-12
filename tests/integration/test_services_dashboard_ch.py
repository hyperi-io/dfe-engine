#  Project:      dfe-engine
#  File:         tests/integration/test_services_dashboard_ch.py
#  Purpose:      Live-CH proof the DFE Services tiles read each pod's series on its own
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The DFE Services dashboard's SQL against real ClickHouse.

Each tile's SQL is read from the shipped dashboard file, its macros expanded as
HyperDX expands them, and run over rows in the real metrics tables. Readings sit
at fixed offsets inside 60-second buckets aligned to the server's clock, so every
tile's answer is exact.

A pod reaches these tables three ways: a scalo-rs push names it in both
``service.instance.id`` and ``k8s.pod.name``, a scalo-py push in
``service.instance.id`` alone, and the collector's scrape in ``k8s.pod.name`` with
the scrape address as the instance id.
"""

import json
import re
from datetime import UTC, datetime

import pytest

from dfe_engine.hyperdx.dashboards import dashboard_files
from dfe_engine.schema.plan import render_one

pytestmark = pytest.mark.integration

_INTERVAL = 60

# HyperDX's own expansions (dfe-hyperdx packages/common-utils/src/macros.ts).
_TIME_FILTER = re.compile(r"\$__timeFilter\((\w+)\)")
_TIME_FILTER_SQL = (
    r"\1 >= toDateTime(fromUnixTimestamp64Milli({startDateMilliseconds:Int64}))"
    r" AND \1 <= toDateTime(fromUnixTimestamp64Milli({endDateMilliseconds:Int64}))"
)
_INTERVAL_SQL = "{intervalSeconds:Int64}"


@pytest.fixture
def otel_db(ch_client, clickhouse_test_database):
    """An isolated database carrying the two metrics tables the dashboard reads."""
    db = clickhouse_test_database
    for object_id in ("data.otel_metrics_gauge", "data.otel_metrics_sum"):
        for statement in render_one(object_id, data_database=db).statements:
            ch_client.command(statement)
    return db


@pytest.fixture
def bucket(ch_client) -> int:
    """The epoch second a 60-second bucket starts at, three buckets before the server's now()."""
    now = int(ch_client.command("SELECT toUnixTimestamp(now())"))
    return now - now % _INTERVAL - 3 * _INTERVAL


def _scalo_rs(pod: str) -> dict[str, str]:
    return {"service.instance.id": pod, "k8s.pod.name": pod}


def _scalo_py(pod: str) -> dict[str, str]:
    return {"service.instance.id": pod}


def _scraped(pod: str) -> dict[str, str]:
    return {"service.instance.id": "10.42.0.7:9090", "k8s.pod.name": pod}


def _write(
    ch_client,
    db: str,
    table: str,
    *points: tuple[int, float],
    service: str,
    metric: str,
    resource: dict[str, str],
    attributes: dict[str, str] | None = None,
) -> None:
    """Write one reading per ``(epoch_second, value)``."""
    attributes = attributes or {}
    resource = {"service.name": service, **resource}
    columns, values = (
        (", AggregationTemporality, IsMonotonic", ", 2, true")
        if table == "otel_metrics_sum"
        else ("", "")
    )
    # clickhouse-connect binds a dict as JSON, which a Map parameter refuses to parse.
    sql = (
        f"INSERT INTO `{db}`.{table} "
        f"(ServiceName, MetricName, Attributes, ResourceAttributes, TimeUnix, Value{columns}) "
        "SELECT {service:String}, {metric:String}, "
        "mapFromArrays({attr_keys:Array(String)}, {attr_values:Array(String)}), "
        "mapFromArrays({res_keys:Array(String)}, {res_values:Array(String)}), "
        "toDateTime({at:UInt32}), {value:Float64}" + values
    )
    for at, value in points:
        ch_client.command(
            sql,
            parameters={
                "service": service,
                "metric": metric,
                "attr_keys": list(attributes),
                "attr_values": list(attributes.values()),
                "res_keys": list(resource),
                "res_values": list(resource.values()),
                "at": at,
                "value": value,
            },
        )


def _tile(tile_id: str) -> dict:
    dashboard = json.loads(dashboard_files()["dfe-services.json"])
    (tile,) = [tile for tile in dashboard["tiles"] if tile["id"] == tile_id]
    return tile["config"]


def _run(ch_client, db: str, tile_id: str, bucket: int) -> list[tuple]:
    """The tile's rows over a range opening one bucket before ``bucket`` and running past now."""
    sql = _TIME_FILTER.sub(_TIME_FILTER_SQL, _tile(tile_id)["sqlTemplate"])
    params = {
        "startDateMilliseconds": (bucket - _INTERVAL) * 1000,
        "endDateMilliseconds": (bucket + 4 * _INTERVAL) * 1000,
    }
    # HyperDX binds the interval only for the time-series display types.
    if "$__interval_s" in sql:
        sql = sql.replace("$__interval_s", _INTERVAL_SQL)
        params["intervalSeconds"] = _INTERVAL
    assert "$__" not in sql, f"{tile_id} uses a macro this test does not expand"
    sql = sql.replace("dfe.otel_metrics_", f"`{db}`.otel_metrics_")
    return ch_client.query(sql, parameters=params).result_rows


def _epoch(value: datetime) -> float:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.timestamp()


# -- counters: each pod differenced on its own, then summed -------------------

# Tile, the counter it reads, the attributes its query selects on, and whether it
# reports a per-second rate rather than a count.
_COUNTER_TILES = [
    ("svc-received", "records_received_total", {}, True),
    ("svc-delivered", "records_delivered_total", {}, True),
    ("svc-loss", "records_dlq_total", {}, False),
    ("svc-scale-events", "worker_pool_scale_events_total", {"direction": "up"}, False),
    ("loader-rows", "rows_inserted_total", {}, True),
    ("loader-inserts", "batches_flushed_total", {}, True),
    ("loader-by-table", "loader_messages_by_table_total", {"table": "auth"}, False),
    ("receiver-requests", "receiver_requests_total", {"transport": "http"}, True),
    ("receiver-bytes", "receiver_bytes_received_total", {}, True),
]


@pytest.mark.parametrize(("tile_id", "metric", "attributes", "per_second"), _COUNTER_TILES)
def test_a_counter_tile_sums_each_pods_own_increase(
    ch_client, otel_db, bucket, tile_id, metric, attributes, per_second
):
    """Pod a adds 30, restarts and counts 20; pod b adds 10: 60 in the bucket.

    Subtracting across the fleet reads pod b's 5 against pod a's 710: 705.
    """
    for pod, points in (
        ("dfe-loader-a", [(bucket + 5, 680.0), (bucket + 25, 710.0), (bucket + 45, 20.0)]),
        ("dfe-loader-b", [(bucket + 12, 5.0), (bucket + 42, 15.0)]),
    ):
        _write(
            ch_client,
            otel_db,
            "otel_metrics_sum",
            *points,
            service="dfe-loader",
            metric=metric,
            resource=_scalo_rs(pod),
            attributes=attributes,
        )

    rows = _run(ch_client, otel_db, tile_id, bucket)

    assert len(rows) == 1, rows
    # A time-series chart reads its x-axis from a DateTime column.
    if _tile(tile_id)["displayType"] in ("line", "stacked_bar"):
        assert _epoch(rows[0][0]) == bucket
    assert rows[0][-1] == pytest.approx(60 / _INTERVAL if per_second else 60)


def test_an_increase_across_a_bucket_boundary_lands_in_the_later_bucket(ch_client, otel_db, bucket):
    """100 at 50s, 130 at 70s: the 30 lands in the second bucket.

    Differencing inside each bucket sees one reading in each and reads no traffic.
    """
    _write(
        ch_client,
        otel_db,
        "otel_metrics_sum",
        (bucket + 50, 100.0),
        (bucket + 70, 130.0),
        service="dfe-receiver",
        metric="records_received_total",
        resource=_scalo_rs("dfe-receiver-a"),
    )

    rows = _run(ch_client, otel_db, "svc-received", bucket)

    assert [(_epoch(ts), service, rate) for ts, service, rate in rows] == [
        (bucket, "dfe-receiver", 0.0),
        (bucket + _INTERVAL, "dfe-receiver", 30 / _INTERVAL),
    ]


def test_the_balance_table_sums_each_series_increase(ch_client, otel_db, bucket):
    """One pod receives 30 on one source and 10 on another; two pods deliver 30 and 10.

    Keyed on the pod alone, the two sources on one pod subtract: 1030 - 50.
    """
    for source, points in (
        ("syslog", [(bucket + 5, 1000.0), (bucket + 35, 1030.0)]),
        ("http", [(bucket + 10, 50.0), (bucket + 40, 60.0)]),
    ):
        _write(
            ch_client,
            otel_db,
            "otel_metrics_sum",
            *points,
            service="dfe-receiver",
            metric="records_received_total",
            resource=_scalo_rs("dfe-receiver-a"),
            attributes={"source": source},
        )
    for pod, points in (
        ("dfe-receiver-a", [(bucket + 5, 680.0), (bucket + 35, 710.0)]),
        ("dfe-receiver-b", [(bucket + 12, 5.0), (bucket + 42, 15.0)]),
    ):
        _write(
            ch_client,
            otel_db,
            "otel_metrics_sum",
            *points,
            service="dfe-receiver",
            metric="records_delivered_total",
            resource=_scalo_rs(pod),
        )

    rows = _run(ch_client, otel_db, "svc-balance", bucket)

    assert rows == [("dfe-receiver", 40, 0, 40, 0)]


# -- pods: named by k8s.pod.name, else service.instance.id --------------------


def _info(
    ch_client,
    db: str,
    at: int,
    *,
    service: str,
    resource: dict[str, str],
    attributes: dict[str, str] | None = None,
) -> None:
    _write(
        ch_client,
        db,
        "otel_metrics_gauge",
        (at, 1.0),
        service=service,
        metric="info",
        resource=resource,
        attributes={"version": "1.22.16", **(attributes or {})},
    )


def _pods(rows: list[tuple]) -> list[tuple[str, int]]:
    return [(service, pods) for service, _, pods, _, _ in rows]


def test_the_inventory_counts_pods_that_push_without_a_pod_name(ch_client, otel_db, bucket):
    """Two dfe-engine pods push ``info`` naming themselves only in service.instance.id.

    Keyed on k8s.pod.name alone, both read as the one empty name.
    """
    for pod in ("dfe-engine-a", "dfe-engine-b"):
        _info(ch_client, otel_db, bucket + 5, service="dfe-engine", resource=_scalo_py(pod))

    rows = _run(ch_client, otel_db, "svc-inventory", bucket)

    assert _pods(rows) == [("dfe-engine", 2)]


def test_a_pod_both_scraped_and_pushed_is_one_pod(ch_client, otel_db, bucket):
    """While the collector still scrapes pods that also push, each pod counts once.

    The scalo-py push carries no k8s.pod.name, so keyed on that alone the engine pod
    reads as two: its scraped name and the empty one.
    """
    for service, pod, pushed in (
        ("dfe-engine", "dfe-engine-0", _scalo_py("dfe-engine-0")),
        ("dfe-loader", "dfe-loader-0", _scalo_rs("dfe-loader-0")),
    ):
        # The scrape also stamps the pod as an attribute.
        _info(
            ch_client,
            otel_db,
            bucket + 5,
            service=service,
            resource=_scraped(pod),
            attributes={"k8s_pod_name": pod},
        )
        _info(ch_client, otel_db, bucket + 7, service=service, resource=pushed)

    rows = _run(ch_client, otel_db, "svc-inventory", bucket)

    assert _pods(rows) == [("dfe-engine", 1), ("dfe-loader", 1)]


def test_uptime_lists_each_pod_that_pushes_without_a_pod_name(ch_client, otel_db, bucket):
    """Two dfe-engine pods report their start time naming themselves only in service.instance.id.

    Keyed on k8s.pod.name alone, both collapse into one row with no pod.
    """
    for pod, started in (("dfe-engine-a", bucket - 1000), ("dfe-engine-b", bucket - 2000)):
        _write(
            ch_client,
            otel_db,
            "otel_metrics_gauge",
            (bucket + 5, float(started)),
            service="dfe-engine",
            metric="process_start_time_seconds",
            resource=_scalo_py(pod),
        )

    rows = _run(ch_client, otel_db, "svc-uptime", bucket)

    assert sorted((service, pod, _epoch(started)) for service, pod, started, _, _ in rows) == [
        ("dfe-engine", "dfe-engine-a", bucket - 1000),
        ("dfe-engine", "dfe-engine-b", bucket - 2000),
    ]
