#  Project:      dfe-engine
#  File:         tests/integration/test_usage_ch.py
#  Purpose:      The usage rollup, the usage API and the Overview dashboard on a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The usage numbers on a real ClickHouse, from telemetry rows to the API and the dashboard.

The engine applies the whole manifest to an empty database, the 17 usage objects
among it, and a second apply changes none of them. A changed materialised view
SELECT is refused as drift, so a checksum that moved between passes would block
every boot after the first.

Then telemetry goes in a few minutes back on the server's clock, the four
refreshable views run, and both routes answer as a platform caller and as callers
held to their orgs. The minute holding the rows may sit either side of a UTC
midnight, so every assertion reads a highest or a total, never a fixed day.
"""

import json
import re
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from dfe_schemas import usage

from dfe_engine.hyperdx.dashboards import dashboard_files
from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import build_plan
from tests.support.clickhouse_cleanup import drop_database_and_policies
from tests.support.held_callers import caller_in_group

pytestmark = pytest.mark.integration

REFRESH_SECONDS = 90
MINUTE_VIEWS = ("usage_minute_cores_mv", "usage_minute_ingest_mv", "usage_minute_events_mv")
DAILY_VIEW = "usage_daily_mv"

ACME_TENANTS = ["t-acme-1", "t-acme-2"]
GAMMA_TENANTS = ["t-gamma"]

# Bytes each org's first-hop counter adds in the minute, and the whole deployment's.
ACME_BYTES, BETA_BYTES, GAMMA_BYTES = 200.0, 1000.0, 7000.0
ALL_BYTES = ACME_BYTES + BETA_BYTES + GAMMA_BYTES

# acme's busiest minute is m1: 0.5 + 2.0. Its tenants' own peaks, 1.0 and 2.0, sum to 3.0.
ACME_PEAK_EPS, BETA_PEAK_EPS, ALL_PEAK_EPS = 2.5, 1.5, 12.5

# Every running container at the one tick, and the cores of the three nodes running them.
CORES_DEPLOYED, NODE_CORES = 10.75, 28.0

# HyperDX's expansion of $__dateFilter (dfe-hyperdx packages/common-utils/src/macros.ts).
_DATE_FILTER = re.compile(r"\$__dateFilter\((\w+)\)")
_DATE_FILTER_SQL = (
    r"\1 >= toDate(fromUnixTimestamp64Milli({startDateMilliseconds:Int64}))"
    r" AND \1 <= toDate(fromUnixTimestamp64Milli({endDateMilliseconds:Int64}))"
)


def _usage_ids(plan) -> list[str]:
    """Every manifest object of the usage rollup, by id."""
    return [obj.id for obj in plan.tables() if obj.id.split(".", 1)[1].startswith("usage_")]


@pytest.fixture
def empty_database(ch_client):
    name = f"dfe_usage_{uuid.uuid4().hex[:8]}"
    yield name
    drop_database_and_policies(client=ch_client, database=name)


def test_the_usage_objects_apply_once_and_then_hold_still(ch_client, empty_database):
    from dfe_engine.settings import load_settings

    settings = load_settings().model_copy(deep=True)
    settings.clickhouse.data_database = empty_database
    plan = build_plan(settings=settings)
    ids = _usage_ids(plan)

    first = apply_plan(ch_client, plan)
    second = apply_plan(ch_client, plan)

    def actions(report) -> dict[str, str]:
        return {o.id: o.action for o in report.outcomes if o.id in ids}

    assert len(ids) == 17, ids
    assert not first.refused, [o.describe() for o in first.refused]
    assert set(actions(first).values()) == {"created"}, actions(first)
    assert set(actions(second).values()) == {"unchanged"}, actions(second)
    assert not second.refused, [o.describe() for o in second.refused]
    names = {plan.by_id(object_id).name for object_id in ids}
    live = {
        str(row[0])
        for row in ch_client.query(
            "SELECT name FROM system.tables WHERE database = {db:String}",
            parameters={"db": empty_database},
        ).result_rows
    }
    assert names <= live, names - live
    refreshes = {
        str(row[0])
        for row in ch_client.query(
            "SELECT view FROM system.view_refreshes WHERE database = {db:String}",
            parameters={"db": empty_database},
        ).result_rows
    }
    assert refreshes == {*MINUTE_VIEWS, DAILY_VIEW}


# -- telemetry in, the views run ------------------------------------------------


def _map(attributes: dict[str, str]) -> str:
    pairs = ", ".join(f"'{key}', '{value}'" for key, value in attributes.items())
    return f"map({pairs})"


def _container(pod: str, container: str, node: str) -> str:
    return _map(
        {
            usage.K8S_POD_UID_ATTRIBUTE: pod,
            usage.K8S_CONTAINER_NAME_ATTRIBUTE: container,
            usage.K8S_NODE_NAME_ATTRIBUTE: node,
        }
    )


def _pod(pod: str, node: str) -> str:
    return _map({usage.K8S_POD_UID_ATTRIBUTE: pod, usage.K8S_NODE_NAME_ATTRIBUTE: node})


def _scalar(client, sql: str) -> Any:
    return client.query(sql).result_rows[0][0]


def _insert_telemetry(client, db: str, m0: str) -> None:
    """Cores at one tick, node CPUs, each org's ingest counter and its events, around minute m0."""
    tick = f"toDateTime('{m0}', 'UTC') + INTERVAL 20 SECOND"
    request, phase = usage.K8S_CPU_REQUEST_METRIC, usage.K8S_POD_PHASE_METRIC
    gauges = [
        (request, _container("pa", "recv", "node-a"), 0.5),
        (request, _container("pa", "side", "node-a"), 0.25),
        (request, _container("pb", "load", "node-b"), 2.0),
        (request, _container("pc", "other", "node-c"), 8.0),
        (phase, _pod("pa", "node-a"), 2),
        (phase, _pod("pb", "node-b"), 2),
        (phase, _pod("pc", "node-c"), 2),
    ]
    client.command(
        f"INSERT INTO `{db}`.otel_metrics_gauge "
        "(ResourceAttributes, ServiceName, MetricName, TimeUnix, Value) VALUES "
        + ", ".join(f"({res}, 'k8s-cluster', '{metric}', {tick}, {v})" for metric, res, v in gauges)
    )

    started = f"toDateTime('{m0}', 'UTC') - INTERVAL 1 HOUR"
    cpus = usage.NODE_LOGICAL_CPU_METRIC
    counter, org = usage.INGESTED_BYTES_METRIC, usage.ORG_ID_ATTRIBUTE
    # (service, metric, resource, attributes, seconds after m0, value)
    sums: list[tuple[str, str, str, str, int, float]] = [
        ("k8s-node", cpus, _map({usage.K8S_NODE_NAME_ATTRIBUTE: node}), "map()", 25, cores)
        for node, cores in (("node-a", 4.0), ("node-b", 8.0), ("node-c", 16.0))
    ]
    r1, r2 = _map({"service.instance.id": "r1"}), _map({"service.instance.id": "r2"})
    sums += [
        ("dfe-receiver", counter, r1, _map({org: "t-acme-1"}), 5, 100),
        ("dfe-receiver", counter, r1, _map({org: "t-acme-1"}), 35, 160),
        ("dfe-receiver", counter, r1, _map({org: "t-acme-2"}), 5, 40),
        ("dfe-receiver", counter, r2, _map({org: "beta"}), 10, BETA_BYTES),
        ("dfe-receiver", counter, r2, _map({org: "t-gamma"}), 10, GAMMA_BYTES),
    ]
    client.command(
        f"INSERT INTO `{db}`.otel_metrics_sum (ResourceAttributes, ServiceName, MetricName, "
        "Attributes, StartTimeUnix, TimeUnix, Value, AggregationTemporality, IsMonotonic) VALUES "
        + ", ".join(
            f"({res}, '{svc}', '{metric}', {attrs}, {started}, "
            f"toDateTime('{m0}', 'UTC') + INTERVAL {offset} SECOND, {v}, 2, true)"
            for svc, metric, res, attrs, offset, v in sums
        )
    )

    # (org, minutes after m0, rows, milliseconds apart): each run fits inside its minute.
    events = [
        ("t-acme-1", 0, 60, 1000),
        ("t-acme-1", 1, 30, 2000),
        ("t-acme-2", 1, 120, 500),
        ("beta", 0, 90, 500),
        ("t-gamma", 0, 600, 100),
    ]
    for tenant, minute, rows, step in events:
        client.command(
            f"INSERT INTO `{db}`.main (_timestamp_load, _timestamp, _org_id, _json) "
            f"SELECT addMilliseconds(toDateTime64('{m0}', 3, 'UTC') + INTERVAL {minute} MINUTE, "
            f"number * {step}) AS t, t, '{tenant}', '{{}}' FROM numbers({rows})"
        )


def _refresh(client, db: str, view: str) -> None:
    """Run one refresh that starts after everything written so far, and wait for it."""
    written = _scalar(client, "SELECT toUnixTimestamp(now())")
    while _scalar(client, "SELECT toUnixTimestamp(now())") <= written:
        time.sleep(0.2)
    after = _scalar(client, "SELECT toUnixTimestamp(now())")
    client.command(f"SYSTEM REFRESH VIEW `{db}`.`{view}`")
    deadline = time.monotonic() + REFRESH_SECONDS
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        rows = client.query(
            "SELECT status, exception, toUnixTimestamp(last_success_time) "
            "FROM system.view_refreshes WHERE database = {db:String} AND view = {view:String}",
            parameters={"db": db, "view": view},
        ).result_rows
        status, exception, last_success = rows[0]
        state = {"status": status, "exception": exception}
        if (last_success or 0) >= after and status != "Running":
            assert exception == "", f"{view}: {exception}"
            return
        time.sleep(0.5)
    pytest.fail(f"{view} did not refresh in {REFRESH_SECONDS}s: {state}")


def _roll_up(client, db: str) -> None:
    m0 = _scalar(client, "SELECT toString(toStartOfMinute(now()) - INTERVAL 4 MINUTE)")
    _insert_telemetry(client, db, m0)
    for view in MINUTE_VIEWS:
        _refresh(client, db, view)
    _refresh(client, db, DAILY_VIEW)


def _orgs(stack: SimpleNamespace) -> None:
    registry = stack.client.app.state.org_registry
    registry.create("acme", org_ids=ACME_TENANTS)
    registry.create("beta")
    registry.create("gamma", org_ids=GAMMA_TENANTS)
    registry.create("delta", org_ids=["t-delta"])


def _held(stack: SimpleNamespace, name: str, orgs: list[str]):
    app = stack.client.app
    return caller_in_group(app, app.state.settings, name, roles=["org_viewer"], org_ids=orgs)


def _highest(body: dict) -> dict[str, tuple[str, float | None]]:
    return {
        entry["metric"]: (
            entry["status"],
            None if entry["highest_7d"] is None else entry["highest_7d"]["value"],
        )
        for entry in body["metrics"]
    }


# -- the dashboard's SQL, as HyperDX runs it ------------------------------------


def _overview_tiles() -> dict[str, dict]:
    dashboard = json.loads(dashboard_files()["dfe-overview.json"])
    return {tile["id"]: tile["config"] for tile in dashboard["tiles"]}


def _run_tile(client, db: str, tile_id: str) -> list[tuple]:
    sql = _DATE_FILTER.sub(_DATE_FILTER_SQL, _overview_tiles()[tile_id]["sqlTemplate"])
    assert "$__" not in sql, f"{tile_id} uses a macro this test does not expand"
    now_ms = int(_scalar(client, "SELECT toUnixTimestamp64Milli(now64(3))"))
    params = {"startDateMilliseconds": now_ms - 30 * 86_400_000, "endDateMilliseconds": now_ms}
    return client.query(sql.replace("dfe.usage_", f"`{db}`.usage_"), parameters=params).result_rows


def _slug(metric: str) -> str:
    return metric.replace("_", "-")


def test_the_whole_deployment_reads_the_same_through_the_api_and_the_dashboard(ch_client, stack):
    db = stack.database
    _orgs(stack)

    before = stack.client.get("/api/v1/usage/overview", headers=stack.headers)
    tiles_before = {
        metric.name: _run_tile(ch_client, db, f"highest-{_slug(metric.name)}")
        for metric in usage.METRICS
    }
    _roll_up(ch_client, db)
    after = stack.client.get("/api/v1/usage/overview", headers=stack.headers)
    gamma = stack.client.get(
        "/api/v1/usage",
        headers=stack.headers,
        params={"metric": usage.INGESTED_BYTES, "org": "gamma"},
    )

    assert before.status_code == 200, before.text
    assert {status for status, _ in _highest(before.json()).values()} == {"not_measured"}
    # maxOrNull over no rows: a tile reads N/A, never a measured 0.
    assert tiles_before == {metric.name: [(None,)] for metric in usage.METRICS}

    assert after.status_code == 200, after.text
    assert after.json()["orgs"] == []
    expected = {
        usage.CORES_DEPLOYED: CORES_DEPLOYED,
        usage.NODE_CORES: NODE_CORES,
        usage.INGESTED_BYTES: ALL_BYTES,
        usage.EVENTS_PER_SECOND: ALL_PEAK_EPS,
    }
    assert _highest(after.json()) == {
        metric: ("measured", pytest.approx(value)) for metric, value in expected.items()
    }
    for metric, value in expected.items():
        [(number,)] = _run_tile(ch_client, db, f"highest-{_slug(metric)}")
        bars = _run_tile(ch_client, db, f"per-day-{_slug(metric)}")
        assert number == pytest.approx(value), metric
        assert max(bar[1] for bar in bars) == pytest.approx(value), metric
        assert all(bar[0].hour == 0 and bar[0].minute == 0 for bar in bars), bars

    assert gamma.status_code == 200, gamma.text
    body = gamma.json()
    assert (body["orgs"], body["status"]) == (["gamma"], "measured")
    assert sum(day["value"] for day in body["series"]) == pytest.approx(GAMMA_BYTES)


def test_a_held_caller_reads_only_its_own_orgs(ch_client, stack):
    _orgs(stack)
    acme = _held(stack, "acme-viewer", ["acme"])
    beta = _held(stack, "beta-viewer", ["beta"])
    delta = _held(stack, "delta-viewer", ["delta"])

    _roll_up(ch_client, stack.database)
    acme_body = acme.get("/api/v1/usage/overview").json()
    beta_body = beta.get("/api/v1/usage/overview").json()
    delta_body = delta.get("/api/v1/usage/overview").json()
    acme_on_gamma = acme.get(
        "/api/v1/usage", params={"metric": usage.INGESTED_BYTES, "org": "gamma"}
    )
    acme_cores = acme.get("/api/v1/usage", params={"metric": usage.CORES_DEPLOYED}).json()

    withheld = {
        usage.CORES_DEPLOYED: ("not_permitted", None),
        usage.NODE_CORES: ("not_permitted", None),
    }
    assert acme_body["orgs"] == ["acme"]
    # Both of acme's tenant ids, and nothing of beta's or gamma's. 2.5 is acme's busiest
    # minute, where summing each tenant's own daily peak would read 3.0.
    assert _highest(acme_body) == {
        **withheld,
        usage.INGESTED_BYTES: ("measured", pytest.approx(ACME_BYTES)),
        usage.EVENTS_PER_SECOND: ("measured", pytest.approx(ACME_PEAK_EPS)),
    }
    assert _highest(beta_body) == {
        **withheld,
        usage.INGESTED_BYTES: ("measured", pytest.approx(BETA_BYTES)),
        usage.EVENTS_PER_SECOND: ("measured", pytest.approx(BETA_PEAK_EPS)),
    }
    assert _highest(delta_body) == {
        **withheld,
        usage.INGESTED_BYTES: ("not_measured", None),
        usage.EVENTS_PER_SECOND: ("not_measured", None),
    }
    assert acme_on_gamma.status_code == 403, acme_on_gamma.text
    assert acme_on_gamma.json()["code"] == "org_not_granted"
    assert (acme_cores["status"], acme_cores["series"]) == ("not_permitted", [])
