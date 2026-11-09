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
from dfe_engine.api.deps import _registries, get_clickhouse_client
from dfe_engine.appmgmt import instances
from dfe_engine.appmgmt.operations import (
    CPU_SECONDS,
    OPEN_FDS,
    RECORDS_DELIVERED,
    RECORDS_DLQ,
    RECORDS_ERROR,
    RECORDS_PROCESSED,
    RECORDS_RECEIVED,
    OperationalReader,
    ResourceBucket,
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
from tests.support.accounts import admin_on_its_own_password, session_token

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

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
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Off the issued password, which refuses every other call until changed.
            admin_on_its_own_password(app)
            token = session_token(
                app, settings, {"sub": "admin", "org_id": "test-org", "roles": ["admin"]}
            )
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
    description: str = "",
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
            "(ServiceName, MetricName, MetricDescription, Attributes, ResourceAttributes, "
            "StartTimeUnix, TimeUnix, Value, AggregationTemporality, IsMonotonic) "
            "SELECT {service:String}, {metric:String}, {description:String}, "
            "mapFromArrays({attr_keys:Array(String)}, {attr_values:Array(String)}), "
            "mapFromArrays({res_keys:Array(String)}, {res_values:Array(String)}), "
            "toDateTime({start:UInt32}), "
            "toDateTime({base:UInt32} - {age:UInt32}), {value:Float64}, 2, true",
            parameters={
                "service": service,
                "metric": metric,
                "description": description,
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


def _total(manager_client, db: str) -> float:
    reader = OperationalReader(manager_client, db)
    return reader.totals(_SERVICE, window_seconds=3600).totals[RECORDS_RECEIVED]


@pytest.mark.parametrize(
    ("streams", "expected"),
    [
        # Two pods from before the hour, each counted from its first reading in it
        (
            [
                ("pod-a", None, "", [(240, 1000.0), (60, 1060.0)]),
                ("pod-b", None, "", [(233, 50.0), (53, 170.0)]),
            ],
            180.0,
        ),
        # A restart in the hour: the new run counts from zero beside the old run's rise
        (
            [
                ("pod-a", 4000, "", [(300, 880.0), (240, 900.0)]),
                ("pod-a", 200, "", [(120, 3.0), (60, 33.0)]),
            ],
            53.0,
        ),
        # A run that started in the hour counts its first export too
        (
            [("pod-a", 300, "", [(240, 40.0), (60, 70.0)])],
            70.0,
        ),
        # A reading older than the hour is left out
        (
            [("pod-a", None, "", [(5000, 100.0), (120, 400.0), (60, 450.0)])],
            50.0,
        ),
        # A scalo-rs pod's counter split by description: both halves count
        (
            [
                ("pod-a", 7200, "described", [(120, 1.0), (60, 2.0)]),
                ("pod-a", 7185, "", [(120, 500.0), (60, 560.0)]),
            ],
            61.0,
        ),
        # A replaced pod's single reading in the hour adds nothing
        (
            [
                ("pod-a", None, "", [(240, 1000.0)]),
                ("pod-b", None, "", [(120, 2.0), (60, 5.0)]),
            ],
            3.0,
        ),
    ],
    ids=[
        "two-pods",
        "restart",
        "started-in-window",
        "older-reading",
        "split-stream",
        "replaced-pod",
    ],
)
def test_a_counter_total_is_its_rise_over_the_window_across_runs(
    ch_client, manager_client, otel_db, base, streams, expected
):
    for pod, started, description, points in streams:
        _write(ch_client, otel_db, base, *points, description=description, pod=pod, started=started)

    assert _total(manager_client, otel_db) == expected


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
    """The receiver reports and receives 100 over 60s, but has processed, delivered, dead-lettered and failed nothing.

    A push carries only counters that have been recorded, so the other four have no
    rows; the scrape served them as 0. An app-specific error counter it never sent
    stays absent.
    """
    _info(ch_client, otel_db, service=_SERVICE, pod="dfe-receiver-a")
    _write(ch_client, otel_db, base, (60, 1000), (30, 1050), (0, 1100), pod="dfe-receiver-a")

    assert _console_rates(console, _SERVICE) == pytest.approx(
        {
            RECORDS_RECEIVED: 100 / 60,
            RECORDS_PROCESSED: 0.0,
            RECORDS_DELIVERED: 0.0,
            RECORDS_DLQ: 0.0,
            RECORDS_ERROR: 0.0,
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


# -- Latest gauges: a stuck stream beside the live one -----------------------


def _gauge(*, age: int = 0, base: int, ch_client, db: str, started: int, value: float) -> None:
    """One ``process_open_fds`` reading ``age`` seconds before ``base``, from a stream begun ``started`` seconds before ``base``."""
    ch_client.command(
        f"INSERT INTO `{db}`.otel_metrics_gauge "
        "(ServiceName, MetricName, ResourceAttributes, StartTimeUnix, TimeUnix, Value) "
        "SELECT {service:String}, {metric:String}, "
        "mapFromArrays(['service.instance.id'], ['pod-a']), "
        "toDateTime({base:UInt32} - {started:UInt32}), "
        "toDateTime({base:UInt32} - {age:UInt32}), {value:Float64}",
        parameters={
            "age": age,
            "base": base,
            "metric": OPEN_FDS,
            "service": _SERVICE,
            "started": started,
            "value": value,
        },
    )


@pytest.mark.parametrize(
    ("stuck", "written_first"),
    [(13.0, "stuck"), (13.0, "live"), (99.0, "stuck"), (99.0, "live")],
    ids=["lower-stuck-first", "lower-live-first", "higher-stuck-first", "higher-live-first"],
)
def test_a_stuck_stream_on_one_pod_never_wins_a_gauge_reading(
    ch_client, manager_client, otel_db, base, stuck, written_first
):
    """A scalo-rs pod exports a gauge's stuck first stream in the same second as its live one."""
    streams = {"stuck": (7200, stuck), "live": (7185, 26.0)}
    other = "live" if written_first == "stuck" else "stuck"
    for name in (written_first, other):
        started, value = streams[name]
        _gauge(base=base, ch_client=ch_client, db=otel_db, started=started, value=value)

    gauges = OperationalReader(manager_client, otel_db).metrics(_SERVICE).gauges

    assert gauges[OPEN_FDS] == 26.0


def test_the_hour_table_reads_only_the_live_stream(ch_client, manager_client, otel_db, base):
    _gauge(base=base, ch_client=ch_client, db=otel_db, started=7200, value=13.0)
    _gauge(base=base, ch_client=ch_client, db=otel_db, started=7185, value=26.0)

    buckets = OperationalReader(manager_client, otel_db).resource_series(
        _SERVICE, window_seconds=3600, bucket_seconds=3600, metrics=(OPEN_FDS,)
    )

    assert [(bucket.minimum, bucket.maximum, bucket.samples) for bucket in buckets] == [
        (26.0, 26.0, 1)
    ]


def test_a_whole_window_read_is_one_bucket_over_every_reading(
    ch_client, manager_client, otel_db, base
):
    # Readings in three different hours, and a stuck stream beside the newest one.
    for age, value in ((5400, 10.0), (1800, 30.0), (0, 20.0)):
        _gauge(age=age, base=base, ch_client=ch_client, db=otel_db, started=7185, value=value)
    _gauge(base=base, ch_client=ch_client, db=otel_db, started=7200, value=13.0)

    buckets = OperationalReader(manager_client, otel_db).resource_series(
        _SERVICE, bucket_seconds=0, metrics=(OPEN_FDS,), window_seconds=7200
    )

    assert [(b.minimum, b.maximum, b.average, b.samples) for b in buckets] == [
        (10.0, 30.0, 20.0, 3)
    ]
    assert buckets[0].bucket_epoch == pytest.approx(base - 7200, abs=5)


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [("_timestamp_load + toIntervalDay(90)", 90 * 86_400), (None, None)],
    ids=["ninety-days", "no-ttl"],
)
def test_a_whole_retention_read_reaches_back_as_far_as_the_gauge_table_keeps(
    ch_client, console, otel_db, ttl, expected
):
    # The fixture renders the table with no TTL.
    if ttl:
        ch_client.command(f"ALTER TABLE `{otel_db}`.otel_metrics_gauge MODIFY TTL {ttl}")

    resp = console.client.get(
        f"/api/v1/apps/{_SERVICE}/{_INSTANCE}/metrics/series",
        headers=console.headers,
        params={"bucket_seconds": 0, "window_seconds": 0},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retention_seconds"] == expected
    assert body["window_seconds"] == (expected or 36_500 * 86_400)
    assert body["bucket_seconds"] == 0


# -- CPU seconds: a cumulative gauge read as cores used ----------------------


def _cpu(
    ch_client,
    db: str,
    base: int,
    *points: tuple[int, float],
    description: str = "",
    pod: str,
    start: int = 0,
) -> None:
    """One cumulative ``process_cpu_seconds_total`` gauge reading per ``(age_seconds, value)``, from a stream begun at epoch ``start``."""
    for age, value in points:
        ch_client.command(
            f"INSERT INTO `{db}`.otel_metrics_gauge "
            "(ServiceName, MetricName, MetricDescription, ResourceAttributes, StartTimeUnix, "
            "TimeUnix, Value) "
            "SELECT {service:String}, {metric:String}, {description:String}, "
            "mapFromArrays(['service.instance.id'], [{pod:String}]), "
            "toDateTime({start:UInt32}), "
            "toDateTime({base:UInt32} - {age:UInt32}), {value:Float64}",
            parameters={
                "age": age,
                "base": base,
                "metric": CPU_SECONDS,
                "description": description,
                "pod": pod,
                "service": _SERVICE,
                "start": start,
                "value": value,
            },
        )


def _cores(manager_client, db: str) -> list[ResourceBucket]:
    buckets = OperationalReader(manager_client, db).resource_series(
        _SERVICE, window_seconds=3600, bucket_seconds=3600, metrics=(CPU_SECONDS,)
    )
    return [bucket for bucket in buckets if bucket.metric == CPU_SECONDS]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        # Two pods exporting in different seconds, each differenced on its own.
        (
            [(300, 1000.0), (240, 1030.0), (180, 1060.0)],
            [(293, 50.0), (233, 170.0)],
            [0.5, 0.5, 2.0],
        ),
        # A restart drops the total: the drop yields no sample, the next interval does.
        ([(300, 900.0), (240, 3.0), (180, 33.0)], [], [0.5]),
        # One reading has nothing before it to difference against.
        ([(300, 1000.0)], [], []),
    ],
    ids=["two-pods", "restart", "single-reading"],
)
def test_cpu_seconds_read_as_cores_used_per_interval(
    ch_client, manager_client, otel_db, base, first, second, expected
):
    _cpu(ch_client, otel_db, base, *first, pod="pod-a")
    _cpu(ch_client, otel_db, base, *second, pod="pod-b")

    buckets = _cores(manager_client, otel_db)

    samples = sum(bucket.samples for bucket in buckets)
    assert samples == len(expected)
    if expected:
        assert min(b.minimum for b in buckets) == pytest.approx(min(expected))
        assert max(b.maximum for b in buckets) == pytest.approx(max(expected))


def test_cpu_cores_come_from_the_live_stream_beside_a_stuck_one(
    ch_client, manager_client, otel_db, base
):
    # Without the newer start winning each timestamp, every step is zero-length or negative and drops out.
    points = ((300, 1000.0), (240, 1030.0), (180, 1060.0))
    _cpu(ch_client, otel_db, base, *points, pod="pod-a", start=base - 7185)
    _cpu(
        ch_client, otel_db, base, (300, 0.5), (240, 0.5), (180, 0.5), pod="pod-a", start=base - 7200
    )

    buckets = _cores(manager_client, otel_db)

    assert sum(bucket.samples for bucket in buckets) == 2
    assert min(b.minimum for b in buckets) == pytest.approx(0.5)
    assert max(b.maximum for b in buckets) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ([(300, 1000.0), (240, 1030.0), (180, 1060.0)], [(293, 50.0), (233, 170.0)], 180.0),
        # The drop is a restart, so the new run counts from zero
        ([(300, 900.0), (240, 3.0), (180, 33.0)], [], 33.0),
        ([(300, 1000.0)], [], 0.0),
    ],
    ids=["two-pods", "restart", "single-reading"],
)
def test_cpu_seconds_total_is_their_rise_over_the_window(
    ch_client, manager_client, otel_db, base, first, second, expected
):
    _cpu(ch_client, otel_db, base, *first, pod="pod-a")
    _cpu(ch_client, otel_db, base, *second, pod="pod-b")

    found = OperationalReader(manager_client, otel_db).totals(_SERVICE, window_seconds=3600)

    assert found.totals[CPU_SECONDS] == pytest.approx(expected)


def test_cpu_seconds_split_by_description_count_each_half_once(
    ch_client, manager_client, otel_db, base
):
    # A scalo-rs pod exports a described half stuck near zero beside the real one
    described = "Total user and system CPU time spent in seconds"
    _cpu(
        ch_client,
        otel_db,
        base,
        (300, 0.03),
        (240, 0.03),
        (180, 0.03),
        description=described,
        pod="pod-a",
    )
    _cpu(ch_client, otel_db, base, (300, 5541.72), (240, 5542.97), (180, 5544.22), pod="pod-a")

    found = OperationalReader(manager_client, otel_db).totals(_SERVICE, window_seconds=3600)

    assert found.totals[CPU_SECONDS] == pytest.approx(2.5)
