#  Project:      dfe-engine
#  File:         appmgmt/operations.py
#  Purpose:      Per-instance state and throughput read from the otel database
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Operational state and metrics for one app instance.

Reads the OTel tables every DFE app already writes to, so nothing has to be scraped
and no app needs a Service to probe - which matters because the transform charts
render none.

This deliberately does NOT reuse ``keda_shim.QueryShim``. That shim must never raise
and never scale up on a bad metric, so it swallows every failure and returns a
last-good integer. An operations API needs the opposite: real floats, several values
per call, and an honest error when the database cannot answer.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dfe_engine import scaling_pressure
from dfe_engine.schema.applier import ttl_from_engine_full
from dfe_engine.yaml_utils import yaml_load

_DAY_SECONDS = 86_400

_QUERIES = Path(__file__).parent / "queries.yaml"

# Bound values land in a SQL identifier position or a bind slot, so anything outside
# a safe identifier charset is refused before it reaches ClickHouse.
_IDENT = re.compile(r"[A-Za-z0-9_-]+")

# Gauges every scalo app emits.
CPU_UTILISATION = "worker_pool_cpu_utilisation"
MEMORY_USAGE = "container_memory_usage_bytes"
MEMORY_LIMIT = "container_memory_limit_bytes"
SATURATION = "worker_pool_saturation"
ACTIVE_THREADS = "worker_pool_active_threads"
OPEN_FDS = "process_open_fds"
# The app metrics group's start time, which scalo-py emits too. The process_ one is scalo-rs only.
START_TIME = "start_time_seconds"
# The canonical bare name. An app may emit the composite under its own namespace,
# so the query matches by rule and reports every spelling under this one name.
SCALING_PRESSURE = scaling_pressure.GAUGE
# scalo registers this through `gauge!`, so it lands in otel_metrics_gauge, but it holds
# cumulative CPU seconds: resource_series reports it as cores used.
CPU_SECONDS = "process_cpu_seconds_total"

# Gauges only some apps emit, read wherever they are present.
ACTIVE_CONNECTIONS = "receiver_active_connections"
CLICKHOUSE_CONNECTED = "clickhouse_connected"
# Per partition, so the reading is the furthest-behind partition.
CONSUMER_LAG = "consumer_lag"
HUNT_BACKLOG = "dfe_hunt_backlog"
PIPELINE_READY = "pipeline_ready"

GAUGE_METRICS = (
    CPU_UTILISATION,
    MEMORY_USAGE,
    MEMORY_LIMIT,
    SATURATION,
    ACTIVE_THREADS,
    OPEN_FDS,
    START_TIME,
    SCALING_PRESSURE,
    CPU_SECONDS,
    ACTIVE_CONNECTIONS,
    CLICKHOUSE_CONNECTED,
    CONSUMER_LAG,
    HUNT_BACKLOG,
    PIPELINE_READY,
)

# Counters forming the scalo pipeline contract, shared by every data-path app.
RECORDS_RECEIVED = "records_received_total"
RECORDS_PROCESSED = "records_processed_total"
RECORDS_DELIVERED = "records_delivered_total"
RECORDS_DLQ = "records_dlq_total"
RECORDS_ERROR = "records_error_total"

COUNTER_METRICS = (
    RECORDS_RECEIVED,
    RECORDS_PROCESSED,
    RECORDS_DELIVERED,
    RECORDS_DLQ,
    RECORDS_ERROR,
)

# Only an app with that sink has these, so a missing one is never read as 0.
CLICKHOUSE_INSERT_ERRORS = "clickhouse_insert_errors_total"
SINK_ERRORS = "sink_errors_total"

ERROR_COUNTERS = (CLICKHOUSE_INSERT_ERRORS, SINK_ERRORS)

# Only the hunt runner records these, so a missing one is never read as 0.
HUNT_CLAIMS = "dfe_hunt_claims_total"
HUNT_CLICKHOUSE_UNAVAILABLE = "dfe_hunt_clickhouse_unavailable_total"
HUNT_DETECTIONS_DROPPED = "dfe_hunt_detections_dropped_total"
HUNT_FIRE_FAILURES = "dfe_hunt_fire_failures_total"
HUNT_OVERRUNS = "dfe_hunt_overruns_total"
HUNT_ROWS_WRITTEN = "dfe_hunt_rows_written_total"
HUNT_RUNS = "dfe_hunt_runs_total"

HUNT_COUNTERS = (
    HUNT_CLAIMS,
    HUNT_CLICKHOUSE_UNAVAILABLE,
    HUNT_DETECTIONS_DROPPED,
    HUNT_FIRE_FAILURES,
    HUNT_OVERRUNS,
    HUNT_ROWS_WRITTEN,
    HUNT_RUNS,
)

# Every counter a rate or total is read for
_READ_COUNTERS = (*COUNTER_METRICS, *ERROR_COUNTERS, *HUNT_COUNTERS)

# Readings derived from an HTTP service's request-duration histogram.
HTTP_REQUESTS = "http_requests_total"
HTTP_SERVER_ERRORS = "http_server_errors_total"
HTTP_P95 = "http_request_duration_p95_seconds"


class MetricsUnavailableError(RuntimeError):
    """Raised when the otel database cannot answer."""


@dataclass(frozen=True, slots=True)
class AppStatus:
    """Whether an instance is reporting, and since when."""

    telemetry_name: str
    reporting: bool
    last_seen_epoch: float | None = None
    started_epoch: float | None = None

    @property
    def uptime_seconds(self) -> float | None:
        """Seconds from start to the last report, or None until both are known."""
        if self.started_epoch is None or self.last_seen_epoch is None:
            return None
        return max(0.0, self.last_seen_epoch - self.started_epoch)


# The resource dimensions a capacity decision is made from.
RESOURCE_METRICS = (
    MEMORY_USAGE,
    MEMORY_LIMIT,
    CPU_UTILISATION,
    CPU_SECONDS,
    SATURATION,
    SCALING_PRESSURE,
)


@dataclass(frozen=True, slots=True)
class ResourceBucket:
    """One time bucket of a metric, aggregated across every pod of the instance."""

    metric: str
    bucket_epoch: float
    minimum: float
    maximum: float
    average: float
    p95: float
    samples: int


@dataclass(frozen=True, slots=True)
class AppMetrics:
    """The instance's current operational readings."""

    telemetry_name: str
    window_seconds: int
    gauges: dict[str, float] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AppTotals:
    """How much the instance did over a window, counted across every run of every pod."""

    telemetry_name: str
    window_seconds: int
    totals: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceSignals:
    """What one source's records are doing, as the loader counts them.

    Either reading is None when the window holds nothing to compute it from: no
    series, or only series with a single sample. A pod that restarted mid-window
    counts from zero after the restart.
    """

    table: str
    window_seconds: int
    records_per_min: float | None = None
    last_seen_epoch: float | None = None


class OperationalReader:
    """Answers per-instance state and metrics from the otel tables."""

    def __init__(self, client: Any, database: str, *, window_seconds: int = 300) -> None:
        self._client = client
        self._database = self._safe_identifier(database)
        self._window = window_seconds
        self._queries = (yaml_load(_QUERIES) or {}).get("queries", {})

    @staticmethod
    def _safe_identifier(value: str) -> str:
        if not _IDENT.fullmatch(value):
            raise ValueError(f"unsafe database identifier: {value!r}")
        return value

    def _run(self, name: str, params: dict[str, Any]) -> list[tuple]:
        cfg = self._queries[name]
        sql = scaling_pressure.apply(str(cfg["sql"]).replace("__DB__", self._database))
        binds: dict[str, Any] = dict(cfg.get("binds", {}))
        binds.setdefault("window_seconds", self._window)
        binds.update(params)
        timeout = int(cfg.get("timeout_seconds", 10))
        try:
            rows = self._client.execute(
                sql, parameters=binds, settings={"max_execution_time": timeout}
            )
        except Exception as exc:
            raise MetricsUnavailableError(f"otel query {name!r} failed: {exc}") from exc
        return list(rows or [])

    def status(self, telemetry_name: str) -> AppStatus:
        """Whether the instance is reporting telemetry, and since when."""
        service = self._safe_identifier(telemetry_name)
        last_seen = self._last_seen(service)
        started = self._gauges(service).get(START_TIME)
        return AppStatus(
            telemetry_name=telemetry_name,
            reporting=last_seen is not None,
            last_seen_epoch=last_seen,
            started_epoch=float(started) if started is not None else None,
        )

    def metrics(self, telemetry_name: str, *, carries_records: bool = False) -> AppMetrics:
        """Current gauge readings and counter rates for the instance.

        A push carries only the counters that have been recorded, so a reporting
        instance that ``carries_records`` reads each pipeline counter it has no rows
        for as 0. Anything else reports only what it recorded.
        """
        service = self._safe_identifier(telemetry_name)
        gauges = self._gauges(service)
        rates = self._rates(service)
        unrecorded = [name for name in COUNTER_METRICS if name not in rates]
        if carries_records and unrecorded and self._last_seen(service) is not None:
            rates = dict.fromkeys(unrecorded, 0.0) | rates
        http_rates, http_gauges = http_readings(
            rows=self._run("http_request_histogram", {"service": service})
        )
        return AppMetrics(
            gauges=gauges | http_gauges,
            rates=rates | http_rates,
            telemetry_name=telemetry_name,
            window_seconds=self._window,
        )

    def totals(
        self, telemetry_name: str, *, carries_records: bool = False, window_seconds: int
    ) -> AppTotals:
        """How much the instance did over the window, counted across every run of every pod.

        CPU time comes from the cumulative gauge scalo reports it as, or the counter a
        scraped app serves, and requests from the request-duration histogram. A
        reporting instance that ``carries_records`` reads each pipeline counter it has
        no rows for as 0.
        """
        service = self._safe_identifier(telemetry_name)
        window = {"service": service, "window_seconds": window_seconds}
        counted = self._run("counter_increases", window | {"names": [*_READ_COUNTERS, CPU_SECONDS]})
        totals = {str(name): float(value) for name, value in counted}
        cpu = self._run("cpu_seconds_increase", window | {"cpu_seconds": CPU_SECONDS})
        if cpu:
            totals[CPU_SECONDS] = totals.get(CPU_SECONDS, 0.0) + float(cpu[0][0])
        unrecorded = [name for name in COUNTER_METRICS if name not in totals]
        if carries_records and unrecorded and self._last_seen(service) is not None:
            totals = dict.fromkeys(unrecorded, 0.0) | totals
        totals |= http_totals(rows=self._run("http_request_histogram", window))
        return AppTotals(
            telemetry_name=telemetry_name, totals=totals, window_seconds=window_seconds
        )

    def resource_series(
        self,
        telemetry_name: str,
        *,
        window_seconds: int,
        bucket_seconds: int,
        metrics: tuple[str, ...] = RESOURCE_METRICS,
    ) -> list[ResourceBucket]:
        """CPU and memory over time, aggregated across every pod of the instance."""
        service = self._safe_identifier(telemetry_name)
        rows = self._run(
            "resource_series",
            {
                "service": service,
                "names": list(metrics),
                "cpu_seconds": CPU_SECONDS,
                "window_seconds": window_seconds,
                "bucket_seconds": bucket_seconds,
            },
        )
        return [
            ResourceBucket(
                metric=str(name),
                bucket_epoch=_epoch(bucket) or 0.0,
                minimum=float(lo),
                maximum=float(hi),
                average=float(mean),
                p95=float(p95),
                samples=int(samples),
            )
            for bucket, name, lo, hi, mean, p95, samples in rows
        ]

    def retention_seconds(self) -> int | None:
        """How long the gauge table keeps a reading, or None when its TTL is unset or not whole days."""
        rows = self._run("gauge_retention", {"database": self._database})
        ttl = ttl_from_engine_full(engine_full=str(rows[0][0])) if rows else None
        days = ttl.days if ttl is not None else None
        return days * _DAY_SECONDS if days else None

    def source_signals(self, table: str, loaders: Sequence[str]) -> SourceSignals:
        """Throughput and last arrival for the table one source's records land in.

        Scoped to the deployed loader instances rather than to a hardcoded app
        name, and to one table: the counter is per TABLE, so a source sharing the
        platform's default landing table gets that table's whole rate. The caller
        reports which of the two it asked for.
        """
        safe_table = self._safe_identifier(table)
        services = [self._safe_identifier(name) for name in loaders]
        if not services:
            # Nothing is deployed to count records against a table, so no series exists.
            return SourceSignals(table=table, window_seconds=self._window)
        rows = self._run("source_landing_counter", {"services": services, "table": safe_table})
        series = [(float(increase), float(span), float(last)) for increase, span, last in rows]
        return SourceSignals(
            table=table,
            window_seconds=self._window,
            records_per_min=_rate_per_minute(series),
            last_seen_epoch=_last_increase(series),
        )

    def _gauges(self, service: str) -> dict[str, float]:
        rows = self._run("latest_gauges", {"service": service, "names": list(GAUGE_METRICS)})
        return {str(name): float(value) for name, value in rows}

    def _rates(self, service: str) -> dict[str, float]:
        rows = self._run("counter_rates", {"service": service, "names": list(_READ_COUNTERS)})
        return {str(name): float(value) for name, value in rows}

    def _last_seen(self, service: str) -> float | None:
        """When the instance last reported, or None when it is not reporting."""
        rows = self._run("liveness", {"service": service})
        # The row count decides it: max() over no matching rows answers the epoch,
        # not NULL, so an instance that never emitted would otherwise read reporting.
        if not rows or int(rows[0][1] or 0) <= 0:
            return None
        return _epoch(rows[0][0])


def _percentile(
    *, bounds: Sequence[float], counts: Sequence[float], quantile: float
) -> float | None:
    """Linear interpolation of *quantile* across histogram buckets. None when empty.

    ``counts`` has one more entry than ``bounds``: the last bucket is everything
    above the top bound, which can only be reported as that bound.
    """
    total = sum(counts)
    if total <= 0:
        return None
    target = quantile * total
    below = 0.0
    for index, count in enumerate(counts):
        if (count > 0) and (below + count >= target):
            if index >= len(bounds):
                return float(bounds[-1]) if bounds else None
            lower = float(bounds[index - 1]) if index > 0 else 0.0
            upper = float(bounds[index])
            return lower + (upper - lower) * (target - below) / count
        below += count
    return None


def http_readings(*, rows: Sequence[tuple]) -> tuple[dict[str, float], dict[str, float]]:
    """Request rate, 5xx rate and p95 duration from ``http_request_histogram`` rows, as ``(rates, gauges)``.

    Each row is one series' first and last cumulative state in the window, so a series' contribution is its last minus its first. A series whose counts went backwards restarted within the window and is left out rather than subtracted. Both dicts are empty when the service reported no histogram.
    """
    if not (rows):
        return {}, {}
    requests = 0.0
    server_errors = 0.0
    span = 1.0
    reference_bounds = None
    buckets = []
    for (
        server_error,
        bounds,
        first_buckets,
        last_buckets,
        first_count,
        last_count,
        span_seconds,
        started_in_window,
    ) in rows:
        # A series born inside the window started from zero, so its first export is counted too.
        if started_in_window:
            first_count = 0
            first_buckets = [0] * len(last_buckets)
        delta = float(last_count) - float(first_count)
        if delta < 0:
            continue
        span = max(span, float(span_seconds))
        requests += delta
        if server_error:
            server_errors += delta
        if reference_bounds is None:
            reference_bounds = [float(bound) for bound in bounds]
            buckets = [0.0] * (len(reference_bounds) + 1)
        if [float(bound) for bound in bounds] != reference_bounds:
            continue
        for index, (first, last) in enumerate(zip(first_buckets, last_buckets, strict=True)):
            buckets[index] += float(last) - float(first)

    rates = {HTTP_REQUESTS: requests / span, HTTP_SERVER_ERRORS: server_errors / span}
    p95 = _percentile(bounds=reference_bounds or [], counts=buckets, quantile=0.95)
    gauges = {} if p95 is None else {HTTP_P95: p95}
    return rates, gauges


def http_totals(*, rows: Sequence[tuple]) -> dict[str, float]:
    """Requests and 5xx responses over the window from ``http_request_histogram`` rows.

    Counted as ``http_readings`` counts them, a series born in the window from zero and one whose count went backwards left out. Empty when the service reported no histogram.
    """
    if not (rows):
        return {}
    requests = 0.0
    server_errors = 0.0
    for server_error, *_, first_count, last_count, _span_seconds, started_in_window in rows:
        delta = float(last_count) - (0.0 if started_in_window else float(first_count))
        if delta < 0:
            continue
        requests += delta
        if server_error:
            server_errors += delta
    return {HTTP_REQUESTS: requests, HTTP_SERVER_ERRORS: server_errors}


def _rate_per_minute(series: Sequence[tuple[float, float, float]]) -> float | None:
    """Records a minute for the fleet: each series' increase over its own span, summed.

    Each entry is one ``source_landing_counter`` row, ``(increase, span_seconds,
    last_increase)``. A series with a single sample spans no time, and with no
    series spanning any there is no rate to report.
    """
    rates = [increase / span for increase, span, _ in series if span > 0]
    if not rates:
        return None
    return sum(rates) * 60.0


def _last_increase(series: Sequence[tuple[float, float, float]]) -> float | None:
    """When any series last moved - a flat counter keeps reporting after traffic stops."""
    latest = max((last for _, _, last in series), default=0.0)
    return latest if latest > 0 else None


def _epoch(value: Any) -> float | None:
    """Coerce a ClickHouse DateTime or numeric to a POSIX timestamp.

    clickhouse-connect returns naive datetimes in UTC, so a naive value is read as
    UTC rather than the host's local time.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.timestamp()
    return float(value)
