#  Project:      dfe-engine
#  File:         hunt_runner/metrics.py
#  Purpose:      The hunt-runner's OTel instrument set, pushed over the wired OTLP
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the hunt runner reports about itself, as scalo metrics.

The runner pod has no HTTP listener, so it is never scraped; the chart wires
``OTEL_EXPORTER_OTLP_ENDPOINT`` and ``OTEL_SERVICE_NAME=dfe-hunt-runner`` into it
and scalo's OpenTelemetry backend pushes on that endpoint. Until this existed the
runner - where hunts actually execute - was the only app in the stack reporting
nothing at all, so a hunt that stopped firing showed up as an absence of detection
rows and nothing else.

The instruments are created through scalo's manager, which applies the ``dfe``
namespace from the deployment contract, so they land as ``dfe_hunt_*``. The metric
names are Prometheus-shaped because scalo's OTel backend passes an unmapped name
through unchanged, which keeps one spelling on both export paths.

``HuntRunnerMetrics()`` with no manager records nothing: that is the state the
one-shot ``materialise`` command and the unit suite run in, and it is why the
runner's own tests need no metrics backend.
"""

from collections.abc import Iterable
from typing import Any

RUNS = "hunt_runs_total"
RUN_DURATION = "hunt_run_duration_seconds"
ROWS_WRITTEN = "hunt_rows_written_total"
OVERRUNS = "hunt_overruns_total"
CLAIMS = "hunt_claims_total"
BACKLOG = "hunt_backlog"
TICK_DURATION = "hunt_tick_duration_seconds"
DETECTIONS_CAPPED = "hunt_detections_capped_total"
DETECTIONS_DROPPED = "hunt_detections_dropped_total"
CLICKHOUSE_UNAVAILABLE = "hunt_clickhouse_unavailable_total"
FIRE_FAILURES = "hunt_fire_failures_total"
FIRES_HELD = "hunt_fires_held_total"
SAFE_HORIZON_DEFERRALS = "hunt_safe_horizon_deferrals_total"
SAFE_HORIZON_DEFERRALS_PER_FIRE = "hunt_safe_horizon_deferrals_per_fire"
SAFE_HORIZON_CAP_REACHED = "hunt_safe_horizon_cap_reached_total"
SAFE_HORIZON_CAP_TARGET_LAG = "hunt_safe_horizon_cap_ran_with_target_lag_total"
SAFE_HORIZON_UNREADABLE = "hunt_safe_horizon_unreadable_total"
SAFE_HORIZON_SECONDS_BEHIND = "hunt_safe_horizon_seconds_behind"
SAFE_HORIZON_QUERY_DURATION = "hunt_safe_horizon_query_duration_seconds"
OVERLAP_ROWS_REREAD = "hunt_overlap_rows_reread_total"
DUPLICATES_SKIPPED = "hunt_duplicates_skipped_total"
DETECTION_LATENCY = "hunt_detection_latency_seconds"


class HuntRunnerMetrics:
    """The runner's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``,
            ``gauge`` and ``histogram``). ``None`` means no backend, and every
            record method below returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._runs = manager.counter(
            RUNS, "Hunt fires executed, by outcome", ["hunt_id", "outcome"]
        )
        self._run_duration = manager.histogram(
            RUN_DURATION, "Seconds one hunt fire took to execute", ["hunt_id"]
        )
        self._rows = manager.counter(ROWS_WRITTEN, "Rows a hunt's statements inserted", ["hunt_id"])
        self._overruns = manager.counter(
            OVERRUNS, "Fires deferred because the previous one still held the lease", ["hunt_id"]
        )
        self._claims = manager.counter(
            CLAIMS, "Claim attempts, by whether this worker won the hunt", ["hunt_id", "outcome"]
        )
        self._backlog = manager.gauge(BACKLOG, "Hunts due and unclaimed right now")
        self._tick_duration = manager.histogram(TICK_DURATION, "Seconds one runner tick took")
        self._capped = manager.counter(
            DETECTIONS_CAPPED,
            "Runs where a rule matched more than its detection cap and was cut to it",
            ["hunt_id", "rule_id"],
        )
        self._dropped = manager.counter(
            DETECTIONS_DROPPED,
            "Matches a rule's detection cap left unwritten",
            ["hunt_id", "rule_id"],
        )
        self._unavailable = manager.counter(
            CLICKHOUSE_UNAVAILABLE,
            "Connects and ticks that found ClickHouse unreachable or refusing the runner's "
            "user, and were retried",
            ["stage", "reason"],
        )
        self._fire_failures = manager.counter(
            FIRE_FAILURES,
            "Hunt fires that failed, by the ClickHouse error category or the hunt's own "
            "refusal (empty_query, no_customers, unknown_customers)",
            ["hunt_id", "reason"],
        )
        self._fires_held = manager.counter(
            FIRES_HELD,
            "Due fires not attempted because the hunt is waiting out a back-off after failing",
            ["hunt_id"],
        )
        self._deferrals = manager.counter(
            SAFE_HORIZON_DEFERRALS,
            "Polls that held a due fire because the safe horizon had not reached it",
            ["hunt_id"],
        )
        self._deferrals_per_fire = manager.histogram(
            SAFE_HORIZON_DEFERRALS_PER_FIRE,
            "Polls a fire was held for the safe horizon before it ran",
            ["hunt_id"],
        )
        self._cap_reached = manager.counter(
            SAFE_HORIZON_CAP_REACHED,
            "Fires run short of the safe horizon because they were held for the deferral cap",
            ["hunt_id"],
        )
        self._cap_target_lag = manager.counter(
            SAFE_HORIZON_CAP_TARGET_LAG,
            "Fires the deferral cap let run while their detection table lagged, so the "
            "dedupe may have let a duplicate through",
            ["hunt_id"],
        )
        self._unreadable = manager.counter(
            SAFE_HORIZON_UNREADABLE,
            "Safe horizon reads that failed, so the fire was held as if the replica lagged",
            ["hunt_id"],
        )
        self._seconds_behind = manager.histogram(
            SAFE_HORIZON_SECONDS_BEHIND,
            "Seconds the safe horizon was short of a due fire, 0 when it had reached it",
            ["hunt_id"],
        )
        self._horizon_duration = manager.histogram(
            SAFE_HORIZON_QUERY_DURATION, "Seconds one safe horizon read took"
        )
        self._reread = manager.counter(
            OVERLAP_ROWS_REREAD,
            "Source rows a fire re-read in the overlap before its watermark",
            ["hunt_id"],
        )
        self._duplicates = manager.counter(
            DUPLICATES_SKIPPED,
            "Detections already recorded in a fire's read window, which it skipped",
            ["hunt_id"],
        )
        self._latency = manager.histogram(
            DETECTION_LATENCY,
            "Seconds from a matched row's _timestamp_load to its detection's insert",
            ["hunt_id"],
        )

    @property
    def enabled(self) -> bool:
        """Whether a backend is wired, so a caller can skip work nothing reads."""
        return self._manager is not None

    def run_completed(self, hunt_id: str, *, duration_seconds: float) -> None:
        """Record a fire that ran its statements and advanced the watermark."""
        if self._manager is None:
            return
        self._runs.labels(hunt_id=hunt_id, outcome="success").inc()
        self._run_duration.labels(hunt_id=hunt_id).observe(duration_seconds)

    def run_failed(self, hunt_id: str, *, duration_seconds: float) -> None:
        """Record a fire that raised, so the watermark did not advance."""
        if self._manager is None:
            return
        self._runs.labels(hunt_id=hunt_id, outcome="failure").inc()
        self._run_duration.labels(hunt_id=hunt_id).observe(duration_seconds)

    def rows_written(self, hunt_id: str, rows: int) -> None:
        """Record what a fire's statements inserted, the hunt's actual output."""
        if self._manager is None:
            return
        self._rows.labels(hunt_id=hunt_id).inc(rows)

    def overrun(self, hunt_id: str) -> None:
        """Record a fire deferred because the hunt was still running."""
        if self._manager is None:
            return
        self._overruns.labels(hunt_id=hunt_id).inc()

    def claim(self, hunt_id: str, *, won: bool) -> None:
        """Record the outcome of a claim - the lease churn between workers."""
        if self._manager is None:
            return
        self._claims.labels(hunt_id=hunt_id, outcome="won" if won else "lost").inc()

    def claim_already_run(self, hunt_id: str) -> None:
        """Record a claim won on a fire a peer had already run, released without running."""
        if self._manager is None:
            return
        self._claims.labels(hunt_id=hunt_id, outcome="already_run").inc()

    def backlog(self, due: int) -> None:
        """Record the due-and-unclaimed count - the same number KEDA scales on."""
        if self._manager is None:
            return
        self._backlog.set(due)

    def tick_completed(self, duration_seconds: float) -> None:
        """Record how long one runner tick took, which is the scheduler's own drift."""
        if self._manager is None:
            return
        self._tick_duration.observe(duration_seconds)

    def detections_capped(self, hunt_id: str, rule_id: str, *, dropped: int) -> None:
        """Record a rule cut to its detection cap, and how many matches it left out."""
        if self._manager is None:
            return
        self._capped.labels(hunt_id=hunt_id, rule_id=rule_id).inc()
        self._dropped.labels(hunt_id=hunt_id, rule_id=rule_id).inc(dropped)

    def clickhouse_unavailable(self, *, stage: str, reason: str) -> None:
        """Record a connect or a tick ClickHouse refused, which the runner retries.

        Args:
            stage: ``connect`` at startup, or ``tick`` once running.
            reason: ``connection`` for an outage, ``authentication`` for a refused user,
                ``capacity`` for a server out of memory or query slots.
        """
        if self._manager is None:
            return
        self._unavailable.labels(stage=stage, reason=reason).inc()

    def fire_failed(self, hunt_id: str, *, reason: str) -> None:
        """Record why a fire failed.

        Args:
            hunt_id: The hunt whose fire failed.
            reason: A ClickHouse error category (``not_found`` for a missing table),
                ``empty_query``, ``no_customers`` or ``unknown_customers``, so the label
                stays bounded.
        """
        if self._manager is None:
            return
        self._fire_failures.labels(hunt_id=hunt_id, reason=reason).inc()

    def fire_held(self, hunt_id: str) -> None:
        """Record a due fire skipped because its hunt is backing off after failing."""
        if self._manager is None:
            return
        self._fires_held.labels(hunt_id=hunt_id).inc()

    def safe_horizon_read(self, duration_seconds: float) -> None:
        """Record how long one safe horizon read took."""
        if self._manager is None:
            return
        self._horizon_duration.observe(duration_seconds)

    def safe_horizon_checked(self, hunt_id: str, seconds_behind: int) -> None:
        """Record how far the safe horizon was short of a due fire."""
        if self._manager is None:
            return
        self._seconds_behind.labels(hunt_id=hunt_id).observe(seconds_behind)

    def safe_horizon_deferred(self, hunt_id: str) -> None:
        """Record a poll that held a due fire for the safe horizon."""
        if self._manager is None:
            return
        self._deferrals.labels(hunt_id=hunt_id).inc()

    def safe_horizon_released(self, hunt_id: str, *, deferrals: int, capped: bool) -> None:
        """Record a held fire let run, after how many polls, and whether the cap let it go."""
        if self._manager is None:
            return
        self._deferrals_per_fire.labels(hunt_id=hunt_id).observe(deferrals)
        if capped:
            self._cap_reached.labels(hunt_id=hunt_id).inc()

    def safe_horizon_cap_ran_with_target_lag(self, hunt_id: str) -> None:
        """Record a capped fire run while its detection table lagged on the answering replica."""
        if self._manager is None:
            return
        self._cap_target_lag.labels(hunt_id=hunt_id).inc()

    def safe_horizon_unreadable(self, hunt_id: str) -> None:
        """Record a safe horizon read that failed, which holds the fire as unknown lag."""
        if self._manager is None:
            return
        self._unreadable.labels(hunt_id=hunt_id).inc()

    def overlap_reread(self, hunt_id: str, *, rows: int, duplicates: int) -> None:
        """Record the rows a fire re-read in its overlap and the detections it skipped."""
        if self._manager is None:
            return
        self._reread.labels(hunt_id=hunt_id).inc(rows)
        self._duplicates.labels(hunt_id=hunt_id).inc(duplicates)

    def detection_latency(self, hunt_id: str, seconds: Iterable[float]) -> None:
        """Record each new detection's delay from its matched row's load to its insert."""
        if self._manager is None:
            return
        histogram = self._latency.labels(hunt_id=hunt_id)
        for value in seconds:
            histogram.observe(value)


def create(app_name: str = "dfe-hunt-runner") -> HuntRunnerMetrics:
    """Build the instrument set on scalo's metrics backend.

    The namespace comes from the engine's deployment contract rather than a literal,
    so the runner's metrics carry the same ``dfe`` prefix as the rest of the product
    (#59). ``OTEL_SERVICE_NAME`` overrides *app_name* on the OTel resource, so the
    chart already decides what ServiceName these rows land under.
    """
    from scalo.metrics import create_metrics

    from dfe_engine.deployment_contract import engine_deployment_contract

    manager = create_metrics(app_name, metric_prefix=engine_deployment_contract().metric_prefix)
    _register_process_gauges(manager)
    return HuntRunnerMetrics(manager)


def _register_process_gauges(manager: Any) -> None:
    """Register scalo's process and container gauges on *manager* with no prefix.

    The dfe-services dashboard reads ``process_resident_memory_bytes`` unprefixed, as
    the other apps emit it, and the manager would otherwise register ``dfe_process_*``.
    """
    from scalo.metrics.groups import ResourceMetrics

    prefix, manager.metric_prefix = manager.metric_prefix, ""
    try:
        ResourceMetrics(mgr=manager)
    finally:
        manager.metric_prefix = prefix


__all__ = [
    "BACKLOG",
    "CLAIMS",
    "CLICKHOUSE_UNAVAILABLE",
    "DETECTIONS_CAPPED",
    "DETECTIONS_DROPPED",
    "DETECTION_LATENCY",
    "DUPLICATES_SKIPPED",
    "FIRES_HELD",
    "FIRE_FAILURES",
    "OVERLAP_ROWS_REREAD",
    "OVERRUNS",
    "ROWS_WRITTEN",
    "RUNS",
    "RUN_DURATION",
    "SAFE_HORIZON_CAP_REACHED",
    "SAFE_HORIZON_CAP_TARGET_LAG",
    "SAFE_HORIZON_DEFERRALS",
    "SAFE_HORIZON_DEFERRALS_PER_FIRE",
    "SAFE_HORIZON_QUERY_DURATION",
    "SAFE_HORIZON_SECONDS_BEHIND",
    "SAFE_HORIZON_UNREADABLE",
    "TICK_DURATION",
    "HuntRunnerMetrics",
    "create",
]
