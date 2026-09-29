#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_metrics.py
#  Purpose:      The instrument set the hunt runner registers, and what it records
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the runner reports about itself, asserted on the values, not the wiring.

The runner pod is never scraped - it pushes over the OTLP endpoint the chart
already wires - so the only thing worth asserting here is the set of instruments it
registers and the observation each record call produces. The ``dfe_`` namespace is
applied by scalo's manager, so it is asserted where it is chosen: in ``create``.
"""

from __future__ import annotations

from dfe_engine.hunt_runner import metrics as hunt_metrics
from dfe_engine.hunt_runner.metrics import HuntRunnerMetrics

from .conftest import Observation, RecordingManager


def test_the_whole_instrument_set_is_registered_up_front(manager):
    HuntRunnerMetrics(manager)

    assert manager.registered == [
        ("counter", "hunt_runs_total"),
        ("histogram", "hunt_run_duration_seconds"),
        ("counter", "hunt_rows_written_total"),
        ("counter", "hunt_overruns_total"),
        ("counter", "hunt_claims_total"),
        ("gauge", "hunt_backlog"),
        ("histogram", "hunt_tick_duration_seconds"),
        ("counter", "hunt_detections_capped_total"),
        ("counter", "hunt_detections_dropped_total"),
    ]


def test_no_backend_registers_nothing_and_records_nothing():
    metrics = HuntRunnerMetrics()

    assert metrics.enabled is False
    # Every record call is a no-op rather than an AttributeError on a missing
    # instrument: this is the state the one-shot materialise command runs in.
    metrics.run_completed("h", duration_seconds=1.0)
    metrics.run_failed("h", duration_seconds=1.0)
    metrics.rows_written("h", 5)
    metrics.overrun("h")
    metrics.claim("h", won=True)
    metrics.backlog(3)
    metrics.tick_completed(0.2)
    metrics.detections_capped("h", "r", dropped=4)


def test_a_completed_fire_counts_once_and_records_its_duration(manager):
    HuntRunnerMetrics(manager).run_completed("brute-force", duration_seconds=2.5)

    assert manager.observed("hunt_runs_total") == [
        Observation("hunt_runs_total", {"hunt_id": "brute-force", "outcome": "success"}, "inc", 1)
    ]
    assert manager.observed("hunt_run_duration_seconds") == [
        Observation("hunt_run_duration_seconds", {"hunt_id": "brute-force"}, "observe", 2.5)
    ]


def test_a_failed_fire_is_the_same_counter_under_a_different_outcome(manager):
    HuntRunnerMetrics(manager).run_failed("brute-force", duration_seconds=0.5)

    assert manager.observed("hunt_runs_total") == [
        Observation("hunt_runs_total", {"hunt_id": "brute-force", "outcome": "failure"}, "inc", 1)
    ]
    # A failure is still timed: a hunt that dies after ten minutes and one that dies
    # immediately are different faults.
    assert manager.observed("hunt_run_duration_seconds") == [
        Observation("hunt_run_duration_seconds", {"hunt_id": "brute-force"}, "observe", 0.5)
    ]


def test_the_rows_a_fire_wrote_are_counted_as_the_hunts_output(manager):
    HuntRunnerMetrics(manager).rows_written("brute-force", 7)

    assert manager.observed("hunt_rows_written_total") == [
        Observation("hunt_rows_written_total", {"hunt_id": "brute-force"}, "inc", 7)
    ]


def test_a_claim_records_whether_this_worker_won_it(manager):
    metrics = HuntRunnerMetrics(manager)

    metrics.claim("a", won=True)
    metrics.claim("b", won=False)

    assert [o.labels for o in manager.observed("hunt_claims_total")] == [
        {"hunt_id": "a", "outcome": "won"},
        {"hunt_id": "b", "outcome": "lost"},
    ]


def test_an_overrun_is_counted_per_hunt(manager):
    HuntRunnerMetrics(manager).overrun("slow-hunt")

    assert manager.observed("hunt_overruns_total") == [
        Observation("hunt_overruns_total", {"hunt_id": "slow-hunt"}, "inc", 1)
    ]


def test_the_backlog_is_set_absolutely_rather_than_incremented(manager):
    metrics = HuntRunnerMetrics(manager)

    metrics.backlog(4)
    metrics.backlog(1)

    assert manager.observed("hunt_backlog") == [
        Observation("hunt_backlog", {}, "set", 4),
        Observation("hunt_backlog", {}, "set", 1),
    ]


def test_a_tick_records_its_own_length(manager):
    HuntRunnerMetrics(manager).tick_completed(0.75)

    assert manager.observed("hunt_tick_duration_seconds") == [
        Observation("hunt_tick_duration_seconds", {}, "observe", 0.75)
    ]


def test_a_capped_rule_counts_once_and_adds_what_it_dropped(manager):
    HuntRunnerMetrics(manager).detections_capped("brute-force", "ssh_fail", dropped=250)

    labels = {"hunt_id": "brute-force", "rule_id": "ssh_fail"}
    assert manager.observed("hunt_detections_capped_total") == [
        Observation("hunt_detections_capped_total", labels, "inc", 1)
    ]
    assert manager.observed("hunt_detections_dropped_total") == [
        Observation("hunt_detections_dropped_total", labels, "inc", 250)
    ]


def test_create_asks_scalo_for_the_products_own_metric_namespace(monkeypatch):
    """The runner's metrics carry the same dfe_ prefix as the rest of the product."""
    asked: dict[str, object] = {}

    def _fake_create_metrics(app_name, metric_prefix=None):
        asked["app_name"] = app_name
        asked["metric_prefix"] = metric_prefix
        return RecordingManager()

    monkeypatch.setattr("scalo.metrics.create_metrics", _fake_create_metrics)

    metrics = hunt_metrics.create()

    assert asked == {"app_name": "dfe-hunt-runner", "metric_prefix": "dfe"}
    assert metrics.enabled is True
