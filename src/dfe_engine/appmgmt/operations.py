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

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

from dfe_engine import scaling_pressure
from dfe_engine.yaml_utils import yaml_load

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
# The app metrics group's start time, which scalo-py emits too; the process_ one is scalo-rs only.
START_TIME = "start_time_seconds"
# The canonical bare name. An app may emit the composite under its own namespace,
# so the query matches by rule and reports every spelling under this one name.
SCALING_PRESSURE = scaling_pressure.GAUGE
# scalo registers this through `gauge!`, so it lands in otel_metrics_gauge and
# holds a CPU percentage rather than cumulative seconds despite the name.
CPU_SECONDS = "process_cpu_seconds_total"

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
)

# Counters forming the scalo pipeline contract, shared by every data-path app.
RECORDS_RECEIVED = "records_received_total"
RECORDS_PROCESSED = "records_processed_total"
RECORDS_DELIVERED = "records_delivered_total"
RECORDS_DLQ = "records_dlq_total"

COUNTER_METRICS = (
    RECORDS_RECEIVED,
    RECORDS_PROCESSED,
    RECORDS_DELIVERED,
    RECORDS_DLQ,
)

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
class SourceSignals:
    """What one source's records are doing, as the loader counts them.

    Either reading is None when the window holds nothing to compute it from -
    an absent series, a single sample, or a counter that only went backwards
    because a pod restarted mid-window.
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
        """Whether the instance is reporting telemetry, and its replica count."""
        service = self._safe_identifier(telemetry_name)
        rows = self._run("liveness", {"service": service})
        # The row count decides it: max() over no matching rows answers the epoch,
        # not NULL, so an instance that never emitted would otherwise read reporting.
        matched = bool(rows) and int(rows[0][1] or 0) > 0
        last_seen = _epoch(rows[0][0]) if matched else None
        started = self._gauges(service).get(START_TIME)
        return AppStatus(
            telemetry_name=telemetry_name,
            reporting=last_seen is not None,
            last_seen_epoch=last_seen,
            started_epoch=float(started) if started is not None else None,
        )

    def metrics(self, telemetry_name: str) -> AppMetrics:
        """Current gauge readings and counter rates for the instance."""
        service = self._safe_identifier(telemetry_name)
        gauges = self._gauges(service)
        rates = self._rates(service)
        http_rates, http_gauges = http_readings(
            rows=self._run("http_request_histogram", {"service": service})
        )
        return AppMetrics(
            gauges=gauges | http_gauges,
            rates=rates | http_rates,
            telemetry_name=telemetry_name,
            window_seconds=self._window,
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
        points = [(_epoch(ts) or 0.0, float(total)) for ts, total in rows]
        return SourceSignals(
            table=table,
            window_seconds=self._window,
            records_per_min=_rate_per_minute(points),
            last_seen_epoch=_last_increase(points),
        )

    def _gauges(self, service: str) -> dict[str, float]:
        rows = self._run("latest_gauges", {"service": service, "names": list(GAUGE_METRICS)})
        return {str(name): float(value) for name, value in rows}

    def _rates(self, service: str) -> dict[str, float]:
        rows = self._run("counter_rates", {"service": service, "names": list(COUNTER_METRICS)})
        return {str(name): float(value) for name, value in rows}


def _percentile(
    *, bounds: Sequence[float], counts: Sequence[float], quantile: float
) -> float | None:
    """Linear interpolation of *quantile* across histogram buckets; None when empty.

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


def _rate_per_minute(points: Sequence[tuple[float, float]]) -> float | None:
    """Records a minute across the window, from the first and last fleet totals.

    A negative delta means a pod restarted and reset its counter, which would
    otherwise report as negative throughput; the window has to roll past the
    restart before the number means anything again.
    """
    if len(points) < 2:
        return None
    elapsed = points[-1][0] - points[0][0]
    delta = points[-1][1] - points[0][1]
    if elapsed <= 0 or delta < 0:
        return None
    return delta / elapsed * 60.0


def _last_increase(points: Sequence[tuple[float, float]]) -> float | None:
    """When the counter last moved - a flat counter keeps reporting after traffic stops."""
    for (_, previous), (timestamp, total) in reversed(list(pairwise(points))):
        if total > previous:
            return timestamp
    return None


def _epoch(value: Any) -> float | None:
    """Coerce a ClickHouse DateTime or numeric to a POSIX timestamp."""
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(value)
