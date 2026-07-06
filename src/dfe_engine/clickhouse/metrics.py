#  Project:      dfe-engine
#  File:         clickhouse/metrics.py
#  Purpose:      Per-query CH observability - structured log always, metrics opt-in
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Per-query ClickHouse observability - a structured log always, metrics opt-in.

Every CH call records a structured event via ``scalo.logger`` (operation, profile,
outcome, duration - pairing with the ``log_comment`` attribution), and - when
metrics are enabled AND a ``scalo.metrics`` backend is installed - a
bounded-cardinality counter + duration histogram. The labels are ``profile`` /
``operation`` / ``outcome`` ONLY: never a table, tenant, or SQL string, which
would explode metric cardinality.

Metrics are best-effort: any backend failure degrades to the log alone and NEVER
surfaces into the query path. They are also opt-in
(``settings.clickhouse.metrics_enabled``, default off) because the metrics backend
+ a ``/metrics`` scrape endpoint are an infra seam the engine does not wire yet
(neither ``prometheus_client`` nor OpenTelemetry is a hard dependency). The
recording code is present + correct so it activates the instant a backend + a
scrape endpoint land; structured logging is the dependency-free signal available
today. Distributed tracing / spans are a separate deferred item (plan 15.10).
"""

from __future__ import annotations

from threading import Lock
from typing import Any

from scalo.logger import logger

# Bounded label sets - low cardinality, safe as metric dimensions.
_QUERY_LABELS = ["profile", "operation", "outcome"]
_DURATION_LABELS = ["profile", "operation"]

_lock = Lock()
_state = "uninit"  # uninit | ready | disabled
_handles: dict[str, Any] | None = None


def _get_handles() -> dict[str, Any] | None:
    """The metric handles, built once when metrics are enabled + a backend exists.

    Returns None when metrics are off (the default) or no backend is installed -
    the caller then records the structured log only.
    """
    global _state, _handles
    if _state == "ready":
        return _handles
    if _state == "disabled":
        return None
    with _lock:
        if _state != "uninit":
            return _handles
        from ..settings import get_settings

        if not get_settings().clickhouse.metrics_enabled:
            _state = "disabled"
            return None
        try:
            from scalo.metrics import create_metrics

            mgr = create_metrics("dfe-engine", backend="prometheus")
            _handles = {
                "queries": mgr.counter(
                    "dfe_clickhouse_queries_total",
                    "ClickHouse queries by profile / operation / outcome",
                    labels=_QUERY_LABELS,
                ),
                "duration": mgr.histogram(
                    "dfe_clickhouse_query_duration_seconds",
                    "ClickHouse query wall-clock duration (seconds)",
                    labels=_DURATION_LABELS,
                ),
            }
            _state = "ready"
            return _handles
        except Exception as exc:  # a missing/broken backend must never break a query
            logger.debug("ClickHouse metrics unavailable - logging only", error=str(exc))
            _state = "disabled"
            return None


def record_query(
    *,
    profile: str,
    operation: str,
    outcome: str,
    duration_s: float,
    rows: int | None = None,
) -> None:
    """Record ONE CH call: a structured log event always, plus metrics when enabled.

    Args:
        profile: the settings profile the call ran on (query / internal / ...).
        operation: the verb (query / command / insert).
        outcome: "ok" or "error".
        duration_s: wall-clock seconds for the call.
        rows: result/affected row count when known (log only - not a metric label).
    """
    # Runs in every query's ``finally`` - it must NEVER raise into the query path,
    # so the whole body (log + settings read + metrics) is guarded.
    try:
        logger.debug(
            "clickhouse.query",
            operation=operation,
            profile=profile,
            outcome=outcome,
            duration_ms=round(duration_s * 1000, 2),
            rows=rows,
        )
        handles = _get_handles()
        if handles is None:
            return
        handles["queries"].labels(profile=profile, operation=operation, outcome=outcome).inc()
        handles["duration"].labels(profile=profile, operation=operation).observe(duration_s)
    except Exception:  # best-effort observability - never break a query
        pass


def reset_metrics_state() -> None:
    """Test hook: forget the lazily-built handles so a changed flag is re-read."""
    global _state, _handles
    with _lock:
        _state = "uninit"
        _handles = None
