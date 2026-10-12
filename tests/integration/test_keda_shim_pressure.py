#  Project:      dfe-engine
#  File:         tests/integration/test_keda_shim_pressure.py
#  Purpose:      Live-CH proof both pressure readers report the hottest pod
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The pressure queries of the KEDA shim and the console against real ClickHouse.

Rows land in the real ``otel_metrics_gauge`` table, rendered from the manifest the
schema phase applies, and ``QueryShim.run`` and ``OperationalReader.metrics`` answer
over them with no mocks. An app's gauge
reaches that table two ways: pushed over OTLP, where scalo puts the pod in the
resource as ``service.instance.id``, and scraped by the collector's Prometheus
receiver, where the pod is a data-point attribute and the resource carries the
scrape target. A push from a scalo release without the resource id carries no pod
at all, so every replica lands in one series.
"""

import pytest

from dfe_engine.appmgmt.operations import SCALING_PRESSURE, OperationalReader
from dfe_engine.keda_shim.shim import QueryShim
from dfe_engine.schema.plan import render_one
from dfe_engine.settings import DFESettings

pytestmark = pytest.mark.integration

_SERVICE = "dfe-receiver"


@pytest.fixture
def gauge_db(ch_client, clickhouse_test_database):
    """An isolated database carrying the real gauge table, dropped after the test."""
    db = clickhouse_test_database
    for statement in render_one("data.otel_metrics_gauge", data_database=db).statements:
        ch_client.command(statement)
    return db


def _shim(ch_client, db: str) -> QueryShim:
    settings = DFESettings(env="test")
    settings.clickhouse.data_database = db
    return QueryShim(settings, client_factory=lambda: ch_client)


def _write(
    ch_client,
    db: str,
    *values: float,
    attributes: dict[str, str] | None = None,
    resource: dict[str, str] | None = None,
    age_seconds: int = 0,
) -> None:
    """Write one gauge reading per value, stamped ``age_seconds`` before the server's now()."""
    attributes = attributes or {}
    resource = resource or {}
    # clickhouse-connect binds a dict as JSON, which a Map parameter refuses to parse.
    for value in values:
        ch_client.command(
            f"INSERT INTO `{db}`.otel_metrics_gauge "
            "(ServiceName, MetricName, Attributes, ResourceAttributes, TimeUnix, Value) "
            "SELECT {service:String}, 'scaling_pressure', "
            "mapFromArrays({attr_keys:Array(String)}, {attr_values:Array(String)}), "
            "mapFromArrays({res_keys:Array(String)}, {res_values:Array(String)}), "
            "now() - INTERVAL {age:UInt32} SECOND, {value:Float64}",
            parameters={
                "service": _SERVICE,
                "attr_keys": list(attributes),
                "attr_values": list(attributes.values()),
                "res_keys": list(resource),
                "res_values": list(resource.values()),
                "age": age_seconds,
                "value": value,
            },
        )


def _pod(name: str) -> dict[str, str]:
    """The resource scalo pushes from one pod."""
    return {"service.name": _SERVICE, "service.instance.id": name, "k8s.pod.name": name}


def test_two_pods_pushing_report_the_hotter_pod(ch_client, gauge_db):
    _write(ch_client, gauge_db, 10, 30, resource=_pod("dfe-receiver-a"))
    _write(ch_client, gauge_db, 70, 90, resource=_pod("dfe-receiver-b"))

    assert _shim(ch_client, gauge_db).run("pressure", {"service": _SERVICE}) == 80


def test_the_console_reads_each_pods_latest_and_reports_the_hotter_pod(
    ch_client, manager_client, otel_db
):
    """Pod a's latest reading is 20 and pod b's is 80, and pod a wrote last."""
    _write(ch_client, otel_db, 90, resource=_pod("dfe-receiver-a"), age_seconds=4)
    _write(ch_client, otel_db, 80, resource=_pod("dfe-receiver-b"), age_seconds=2)
    _write(ch_client, otel_db, 20, resource=_pod("dfe-receiver-a"))

    gauges = OperationalReader(manager_client, otel_db).metrics(_SERVICE).gauges

    assert gauges[SCALING_PRESSURE] == 80


def test_pushed_without_a_pod_beside_scraped_per_pod_reads_the_hottest_series(ch_client, gauge_db):
    """The merged push averages to 40 and the scraped pods to 35 and 50."""
    _write(ch_client, gauge_db, 30, 50, resource={"service.name": _SERVICE})
    _write(
        ch_client,
        gauge_db,
        35,
        attributes={"k8s_pod_name": "dfe-receiver-a"},
        resource={"service.name": _SERVICE, "service.instance.id": "192.0.2.10:9090"},
    )
    _write(
        ch_client,
        gauge_db,
        45,
        55,
        attributes={"k8s_pod_name": "dfe-receiver-b"},
        resource={"service.name": _SERVICE, "service.instance.id": "192.0.2.11:9090"},
    )

    assert _shim(ch_client, gauge_db).run("pressure", {"service": _SERVICE}) == 50


def test_no_gauge_reads_zero_and_only_a_query_error_holds_the_last_good_value(ch_client, gauge_db):
    shim = _shim(ch_client, gauge_db)
    assert shim.run("pressure", {"service": _SERVICE}) == 0

    _write(ch_client, gauge_db, 80, resource=_pod("dfe-receiver-a"))
    assert shim.run("pressure", {"service": _SERVICE}) == 80

    ch_client.command(f"DROP TABLE `{gauge_db}`.otel_metrics_gauge SYNC")
    assert shim.run("pressure", {"service": _SERVICE}) == 80

    for statement in render_one("data.otel_metrics_gauge", data_database=gauge_db).statements:
        ch_client.command(statement)
    assert shim.run("pressure", {"service": _SERVICE}) == 0
