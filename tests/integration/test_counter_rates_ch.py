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

A cumulative stream is one metric, attribute set and pod from one ``StartTimeUnix``.
A pod can push two streams under the same names, so every reading here names its
stream's start, and rows written without one share the epoch as theirs.

The endpoint tests go through ``GET /api/v1/apps/{service}/{instance}/metrics`` on
the real API, which is where the manifest's ``transports`` decides whether a
counter with no rows reads as 0.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token, get_clickhouse_client
from dfe_engine.appmgmt import instances
from dfe_engine.appmgmt.operations import (
    RECORDS_DELIVERED,
    RECORDS_DLQ,
    RECORDS_PROCESSED,
    RECORDS_RECEIVED,
    OperationalReader,
)
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseResilienceSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    KafkaSettings,
    LocalAuthSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)
from tests.support.accounts import admin_on_its_own_password

pytestmark = pytest.mark.integration

_SERVICE = "dfe-receiver"
_LOADER = "dfe-loader"
_LANDING = "loader_messages_by_table_total"
_TABLE = "auth"
_INSTANCE = "default"
# The instances the console fixture deploys, so their metrics routes resolve.
_DEPLOYED = (_SERVICE, "dfe-engine")
# Stream starts, as seconds before the test's ``base``: both well before the window.
_EARLY = 3600
_LATER = 1800


@pytest.fixture
def base(ch_client) -> int:
    """The server's now() as an epoch second, read once for the whole test."""
    return int(ch_client.command("SELECT toUnixTimestamp(now())"))


@pytest.fixture
def console(ch_params, manager_client, otel_db, monkeypatch, tmp_path):
    """The engine API, reading its telemetry from ``otel_db`` on the harness's ClickHouse."""
    for sub in ("sources", "services", "rules", "hunts", "auth", "secrets"):
        (tmp_path / sub).mkdir()
    settings = DFESettings(
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(
            host=ch_params["host"],
            port=ch_params["port"],
            username=ch_params["username"],
            password=ch_params["password"],
            secure=False,
            data_database=otel_db,
            bootstrap_tables=False,
            resilience=ClickHouseResilienceSettings(budget_seconds=5.0),
        ),
        kafka=KafkaSettings(ensure_topics=False),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(admin_password="test-admin-pw"),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )
    monkeypatch.setattr("dfe_engine.settings._settings", settings)
    app = create_app(settings=settings)
    app.dependency_overrides[get_clickhouse_client] = lambda: manager_client
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]}, settings=settings
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Off the issued password, which refuses every other call until changed.
            admin_on_its_own_password(app)
            gc = GitCrud(
                GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
            )
            for service in _DEPLOYED:
                app_instance = instances.instance_of(service, _INSTANCE)
                gc.put(
                    instances.HELMVARS_CLASS,
                    app_instance.overlay_name,
                    instances.initial_overlay(app_instance),
                    "tester",
                )
            app.state.gitcrud = gc
            yield SimpleNamespace(client=client, headers={"Authorization": f"Bearer {token}"})
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()


def _write(
    ch_client,
    db: str,
    base: int,
    *points: tuple[int, float],
    pod: str,
    service: str = _SERVICE,
    metric: str = RECORDS_RECEIVED,
    attributes: dict[str, str] | None = None,
    started: int | None = None,
) -> None:
    """Write one cumulative reading per ``(age_seconds, value)``, stamped that long before ``base``.

    ``started`` is how long before ``base`` the stream began; None leaves its start at
    the epoch, as a row that carries no start time reads.
    """
    attributes = attributes or {}
    resource = {"service.name": service, "service.instance.id": pod, "k8s.pod.name": pod}
    start = 0 if started is None else base - started
    # clickhouse-connect binds a dict as JSON, which a Map parameter refuses to parse.
    for age, value in points:
        ch_client.command(
            f"INSERT INTO `{db}`.otel_metrics_sum "
            "(ServiceName, MetricName, Attributes, ResourceAttributes, StartTimeUnix, "
            "TimeUnix, Value, AggregationTemporality, IsMonotonic) "
            "SELECT {service:String}, {metric:String}, "
            "mapFromArrays({attr_keys:Array(String)}, {attr_values:Array(String)}), "
            "mapFromArrays({res_keys:Array(String)}, {res_values:Array(String)}), "
            "toDateTime({start:UInt32}), "
            "toDateTime({base:UInt32} - {age:UInt32}), {value:Float64}, 2, true",
            parameters={
                "service": service,
                "metric": metric,
                "attr_keys": list(attributes),
                "attr_values": list(attributes.values()),
                "res_keys": list(resource),
                "res_values": list(resource.values()),
                "start": start,
                "base": base,
                "age": age,
                "value": value,
            },
        )


def _rate(manager_client, db: str) -> float:
    return OperationalReader(manager_client, db).metrics(_SERVICE).rates[RECORDS_RECEIVED]


def _landing(
    ch_client, db: str, base: int, *points: tuple[int, float], pod: str, started: int | None = None
) -> None:
    _write(
        ch_client,
        db,
        base,
        *points,
        pod=pod,
        service=_LOADER,
        metric=_LANDING,
        attributes={"table": _TABLE},
        started=started,
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


def test_a_counter_reset_inside_one_stream_counts_from_zero(
    ch_client, manager_client, otel_db, base
):
    """1000 -> 1040, then a drop to 20 -> 60 under the same start: 40 + 20 + 40 = 100 over 60s.

    Differencing the first reading against the last reads (60 - 1000) / 60 = -15.67.
    """
    _write(
        ch_client,
        otel_db,
        base,
        (60, 1000),
        (40, 1040),
        (20, 20),
        (0, 60),
        pod="dfe-receiver-a",
        started=_EARLY,
    )

    assert _rate(manager_client, otel_db) == pytest.approx(100 / 60, rel=1e-3)


def test_two_streams_on_one_pod_exporting_in_turn_read_their_combined_rate(
    ch_client, manager_client, otel_db, base
):
    """One pod pushes two streams of one counter: 100 over 60s and 20 over 60s, 2 a second.

    Keyed without the start time, the readings interleave as 20, 1000, 25, 1050, 35,
    40, 1100, and every step down reads as a restart: 3130 / 60 = 52.17.
    """
    _write(
        ch_client,
        otel_db,
        base,
        (60, 1000),
        (30, 1050),
        (0, 1100),
        pod="dfe-receiver-a",
        started=_EARLY,
    )
    _write(
        ch_client,
        otel_db,
        base,
        (60, 20),
        (45, 25),
        (15, 35),
        (0, 40),
        pod="dfe-receiver-a",
        started=_LATER,
    )

    assert _rate(manager_client, otel_db) == pytest.approx(120 / 60, rel=1e-3)


def test_a_restart_under_a_new_start_is_read_over_the_pods_whole_span(
    ch_client, manager_client, otel_db, base
):
    """1000 -> 1040, then a new stream started 25s ago reads 20 -> 60: 80 over 60s.

    The new stream's first reading is its baseline, so the 20 it counted before its
    first export is not in the window. Reading each stream over its own span instead
    gives 40 / 20 + 40 / 20 = 4.
    """
    _write(
        ch_client,
        otel_db,
        base,
        (60, 1000),
        (40, 1040),
        pod="dfe-receiver-a",
        started=_EARLY,
    )
    _write(ch_client, otel_db, base, (20, 20), (0, 60), pod="dfe-receiver-a", started=25)

    assert _rate(manager_client, otel_db) == pytest.approx(80 / 60, rel=1e-3)


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


def test_two_streams_on_one_loader_pod_read_their_combined_records_a_minute(
    ch_client, manager_client, otel_db, base
):
    """Each export carries both streams: 100 and 20 over 60s, 120 a minute.

    Keyed without the start time, each export reads 20 then 1000 and every step
    down is a restart: 3130 records over 60s.
    """
    _landing(
        ch_client,
        otel_db,
        base,
        (60, 1000),
        (30, 1050),
        (0, 1100),
        pod="dfe-loader-a",
        started=_EARLY,
    )
    _landing(
        ch_client, otel_db, base, (60, 20), (30, 30), (0, 40), pod="dfe-loader-a", started=_LATER
    )

    signals = OperationalReader(manager_client, otel_db).source_signals(_TABLE, [_LOADER])

    assert signals.records_per_min == pytest.approx(120.0, rel=1e-3)
    assert signals.last_seen_epoch == float(base)


def test_flat_streams_on_one_loader_pod_report_no_arrival(ch_client, manager_client, otel_db, base):
    """One stream holds at 2 and another at 40, so nothing arrived.

    Keyed without the start time, each export reads 2 then 40 and calls it a rise.
    """
    _landing(ch_client, otel_db, base, (60, 2), (30, 2), (0, 2), pod="dfe-loader-a", started=_EARLY)
    _landing(
        ch_client, otel_db, base, (60, 40), (30, 40), (0, 40), pod="dfe-loader-a", started=_LATER
    )

    signals = OperationalReader(manager_client, otel_db).source_signals(_TABLE, [_LOADER])

    assert signals.records_per_min == 0.0
    assert signals.last_seen_epoch is None


# -- the console endpoint: counters an app has not recorded ------------------


def _info(ch_client, db: str, *, service: str, pod: str) -> None:
    """One ``info`` reading now, which is what the liveness query reads as reporting."""
    ch_client.command(
        f"INSERT INTO `{db}`.otel_metrics_gauge "
        "(ServiceName, MetricName, ResourceAttributes, TimeUnix, Value) "
        "SELECT {service:String}, 'info', "
        "mapFromArrays(['service.instance.id'], [{pod:String}]), now(), 1",
        parameters={"service": service, "pod": pod},
    )


def _console_rates(console, service: str) -> dict[str, float]:
    resp = console.client.get(
        f"/api/v1/apps/{service}/{_INSTANCE}/metrics", headers=console.headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["rates"]


def test_a_reporting_app_on_the_data_path_reads_an_unrecorded_counter_as_zero(
    ch_client, otel_db, base, console
):
    """The receiver reports and receives 100 over 60s, but has processed, delivered and dead-lettered nothing.

    A push carries only counters that have been recorded, so the other three have no
    rows; the scrape served them as 0.
    """
    _info(ch_client, otel_db, service=_SERVICE, pod="dfe-receiver-a")
    _write(ch_client, otel_db, base, (60, 1000), (30, 1050), (0, 1100), pod="dfe-receiver-a")

    assert _console_rates(console, _SERVICE) == pytest.approx(
        {
            RECORDS_RECEIVED: 100 / 60,
            RECORDS_PROCESSED: 0.0,
            RECORDS_DELIVERED: 0.0,
            RECORDS_DLQ: 0.0,
        },
        rel=1e-3,
    )


def test_a_reporting_app_with_no_transport_reads_only_what_it_recorded(ch_client, otel_db, console):
    """dfe-engine declares ``transports: []``: it carries no records, so it gains no record counters."""
    _info(ch_client, otel_db, service="dfe-engine", pod="dfe-engine-a")

    assert _console_rates(console, "dfe-engine") == {}


def test_an_app_that_is_not_reporting_reads_no_counters(console):
    """No ``info`` in the window: the receiver is not reporting, so nothing reads as 0."""
    assert _console_rates(console, _SERVICE) == {}


def test_an_instance_that_is_not_deployed_is_not_found(ch_client, otel_db, console):
    """The receiver reports under its own name whatever instance is asked for, so only the overlay says ``absent`` is not deployed."""
    _info(ch_client, otel_db, service=_SERVICE, pod="dfe-receiver-a")

    resp = console.client.get(f"/api/v1/apps/{_SERVICE}/absent/metrics", headers=console.headers)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "not_deployed"
