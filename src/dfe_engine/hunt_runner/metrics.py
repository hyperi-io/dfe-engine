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


def create(app_name: str = "dfe-hunt-runner") -> HuntRunnerMetrics:
    """Build the instrument set on scalo's metrics backend.

    The namespace comes from the engine's deployment contract rather than a literal,
    so the runner's metrics carry the same ``dfe`` prefix as the rest of the product
    (#59). ``OTEL_SERVICE_NAME`` overrides *app_name* on the OTel resource, so the
    chart already decides what ServiceName these rows land under.
    """
    from scalo.metrics import create_metrics

    from dfe_engine.deployment_contract import engine_deployment_contract

    return HuntRunnerMetrics(
        create_metrics(app_name, metric_prefix=engine_deployment_contract().metric_prefix)
    )


__all__ = [
    "BACKLOG",
    "CLAIMS",
    "DETECTIONS_CAPPED",
    "DETECTIONS_DROPPED",
    "OVERRUNS",
    "ROWS_WRITTEN",
    "RUNS",
    "RUN_DURATION",
    "TICK_DURATION",
    "HuntRunnerMetrics",
    "create",
]
