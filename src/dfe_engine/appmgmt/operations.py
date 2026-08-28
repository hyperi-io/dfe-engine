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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
START_TIME = "process_start_time_seconds"
SCALING_PRESSURE = "dfe_scaling_pressure"
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
        sql = str(cfg["sql"]).replace("__DB__", self._database)
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
        last_seen = _epoch(rows[0][0]) if rows and rows[0][0] is not None else None
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
        return AppMetrics(
            telemetry_name=telemetry_name,
            window_seconds=self._window,
            gauges=self._gauges(service),
            rates=self._rates(service),
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

    def _gauges(self, service: str) -> dict[str, float]:
        rows = self._run("latest_gauges", {"service": service, "names": list(GAUGE_METRICS)})
        return {str(name): float(value) for name, value in rows}

    def _rates(self, service: str) -> dict[str, float]:
        rows = self._run("counter_rates", {"service": service, "names": list(COUNTER_METRICS)})
        return {str(name): float(value) for name, value in rows}


def _epoch(value: Any) -> float | None:
    """Coerce a ClickHouse DateTime or numeric to a POSIX timestamp."""
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return float(value)
