#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_runner.py
#  Purpose:      Tests for hunt-runner load-spread, double-run decision, heartbeat
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deterministic spread + scheduling-decision tests, and the tick's heartbeat."""

from __future__ import annotations

from scalo.logger import logger

from dfe_engine.hunt_runner import HuntState, decide, next_due, phase_offset
from dfe_engine.hunt_runner.ch_coordinator import Lease
from dfe_engine.hunt_runner.metrics import HuntRunnerMetrics
from dfe_engine.hunt_runner.runner import HuntRunner
from dfe_engine.hunt_runner.scheduler import mark_deferred

from .conftest import Observation


def test_phase_offset_is_stable_and_within_window():
    a = phase_offset("hunt-a", 600)
    assert a == phase_offset("hunt-a", 600)  # stable across calls
    assert 0 <= a < int(600 * 0.8)


def test_phase_offset_spreads_distinct_hunts():
    offsets = {phase_offset(f"hunt-{i}", 600) for i in range(50)}
    # hash-based spread -> many distinct offsets across the window
    assert len(offsets) > 25


def test_next_due_is_future_and_within_interval():
    now = 1_000_000
    due = next_due("hunt-a", 600, now)
    assert due > now
    assert due - now <= 600


def test_decide_not_due_waits():
    st = HuntState(hunt_id="h")
    d = decide(st, now_epoch=100, due_epoch=200, running_count=0, cap=5)
    assert d.action == "wait"
    assert d.reason == "not_due"


def test_decide_running_defers_never_double_runs():
    st = HuntState(hunt_id="h", status="running")
    d = decide(st, now_epoch=300, due_epoch=200, running_count=1, cap=5)
    assert d.action == "defer"


def test_decide_cap_reached_waits():
    st = HuntState(hunt_id="h", status="idle")
    d = decide(st, now_epoch=300, due_epoch=200, running_count=5, cap=5)
    assert d.action == "wait"
    assert d.reason == "cap_reached"


def test_decide_runs_when_due_idle_and_under_cap():
    st = HuntState(hunt_id="h", status="idle")
    d = decide(st, now_epoch=300, due_epoch=200, running_count=2, cap=5)
    assert d.action == "run"


def test_latest_fire_is_this_interval_once_arrived_else_the_previous():
    from dfe_engine.hunt_runner.spread import current_fire, latest_fire

    # fire = boundary + stable offset; owed from the moment now passes it
    fire = current_fire("h", 600, 1000)
    assert 600 <= fire < 1200
    assert latest_fire("h", 600, fire) == fire
    assert latest_fire("h", 600, fire - 1) == fire - 600


def test_a_hunt_whose_offset_falls_between_ticks_still_runs_every_interval():
    """A 15s poll ticking at :00/:15/:30/:45 never lands inside [46, 60)."""
    from dfe_engine.hunt_runner.models import HuntSpec

    hunt_id = "post-1682300-71da5fa4"
    assert phase_offset(hunt_id, 60) == 46
    coord = _WatermarkCoordinator()
    worker = _CountingWorker()
    spec = HuntSpec(hunt_id=hunt_id, interval_seconds=60, queries=["SELECT 1"])
    runner = HuntRunner(coord, worker, {hunt_id: spec}, cap=8)
    base = 1_789_027_260
    ticks = [base + minute * 60 + phase for minute in range(3) for phase in (0, 15, 30, 45)]
    for now in ticks:
        runner.tick(now)
    # Once per interval, never twice, and never zero.
    assert worker.runs == 3
    assert coord.watermarks[hunt_id] == base + 2 * 60 + 46 - 60


def test_mark_deferred_flags_too_aggressive():
    st = HuntState(hunt_id="h", status="running", overrun_count=1)
    out = mark_deferred(st)
    assert out.too_aggressive is True
    assert out.overrun_count == 2
    assert out.status == "deferred"


class _RecordingCoordinator:
    """A coordinator that records call order and grants every claim."""

    def __init__(self, *, heartbeat_raises: bool = False, backlog_raises: bool = False) -> None:
        self.calls: list[str] = []
        self.beats: list[tuple[int, float]] = []
        self.backlog = 0
        self._heartbeat_raises = heartbeat_raises
        self._backlog_raises = backlog_raises

    def heartbeat(self, now: int, poll_seconds: float) -> None:
        self.calls.append("heartbeat")
        if self._heartbeat_raises:
            raise RuntimeError("clickhouse is down")
        self.beats.append((now, poll_seconds))

    def backlog_count(self) -> int:
        self.calls.append("backlog_count")
        if self._backlog_raises:
            raise RuntimeError("clickhouse is down")
        return self.backlog

    def active_count(self, now: int) -> int:
        self.calls.append("active_count")
        return 0

    def pending_runs(self, now: int) -> dict[str, int]:
        self.calls.append("pending_runs")
        return {}

    def get_watermark(self, hunt_id: str) -> int | None:
        return None

    def current_lease(self, hunt_id: str):
        return None

    def try_claim(self, hunt_id: str, fire: int, now: int) -> bool:
        self.calls.append("try_claim")
        return True

    def release(self, hunt_id: str, fire: int) -> None:
        self.calls.append("release")


class _WatermarkCoordinator(_RecordingCoordinator):
    """A coordinator whose watermark reaches the fire before the lease is released.

    That is the worker's order: it commits the watermark, then the runner releases.
    """

    def __init__(self) -> None:
        super().__init__()
        self.watermarks: dict[str, int] = {}

    def get_watermark(self, hunt_id: str) -> int | None:
        return self.watermarks.get(hunt_id)

    def release(self, hunt_id: str, fire: int) -> None:
        self.calls.append("release")
        self.watermarks[hunt_id] = fire


class _PeerRanItCoordinator(_WatermarkCoordinator):
    """A peer runs the fire and releases it between this runner's watermark read and its claim."""

    def try_claim(self, hunt_id: str, fire: int, now: int) -> bool:
        self.calls.append("try_claim")
        self.watermarks[hunt_id] = fire
        return True


class _LeasedCoordinator(_RecordingCoordinator):
    """A coordinator whose hunt is already running under a live lease."""

    def current_lease(self, hunt_id: str) -> Lease:
        return Lease(hunt_id=hunt_id, owner="another-pod", fire=0, lease_until=2**40)

    def record_overrun(self, hunt_id: str) -> None:
        self.calls.append("record_overrun")


class _LosingCoordinator(_RecordingCoordinator):
    """A coordinator that loses every settle-window race to another worker."""

    def try_claim(self, hunt_id: str, fire: int, now: int) -> bool:
        self.calls.append("try_claim")
        return False


class _CountingWorker:
    """A worker that runs nothing and only counts."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self, spec, fire: int) -> None:
        self.runs += 1


def _due_runner(
    coord, worker, *, poll_seconds: float = 15.0, metrics=None
) -> tuple[HuntRunner, int]:
    """A runner holding one hunt, plus a *now* at which that hunt is due."""
    from dfe_engine.hunt_runner.models import HuntSpec

    spec = HuntSpec(hunt_id="h", interval_seconds=600, queries=["SELECT 1"])
    # The fire is the interval boundary plus the hunt's stable offset, so a now on
    # that instant is due without any dependence on the real clock.
    now = 600_000 + phase_offset("h", 600)
    runner = HuntRunner(
        coord, worker, {"h": spec}, cap=8, poll_seconds=poll_seconds, metrics=metrics
    )
    return runner, now


def test_a_tick_beats_before_it_claims_anything():
    coord = _RecordingCoordinator()
    worker = _CountingWorker()
    runner, now = _due_runner(coord, worker, poll_seconds=30.0)
    assert runner.tick(now) == 1
    assert coord.calls[0] == "heartbeat"
    assert coord.calls.index("heartbeat") < coord.calls.index("try_claim")
    # The beat carries the tick's own clock and the cadence the runner was built on.
    assert coord.beats == [(now, 30.0)]


def test_a_tick_with_nothing_due_still_beats():
    coord = _RecordingCoordinator()
    runner = HuntRunner(coord, _CountingWorker(), {}, cap=8, poll_seconds=15.0)
    assert runner.tick(1000) == 0
    assert coord.beats == [(1000, 15.0)]


def test_a_failed_beat_does_not_cost_the_runs():
    coord = _RecordingCoordinator(heartbeat_raises=True)
    worker = _CountingWorker()
    runner, now = _due_runner(coord, worker)
    assert runner.tick(now) == 1
    assert worker.runs == 1


class _FailingWorker:
    """A worker whose hunt raises, as a bad statement or a dropped table would."""

    def run(self, spec, fire: int) -> None:
        raise RuntimeError("clickhouse rejected the insert")


def test_a_tick_publishes_the_backlog_the_shim_scales_on(manager):
    coord = _RecordingCoordinator()
    coord.backlog = 4
    runner = HuntRunner(coord, _CountingWorker(), {}, cap=8, metrics=HuntRunnerMetrics(manager))

    runner.tick(1000)

    assert manager.observed("hunt_backlog") == [Observation("hunt_backlog", {}, "set", 4)]


def test_a_runner_with_no_metrics_backend_does_not_query_the_backlog():
    """The backlog is another ClickHouse round trip per tick; nothing reads it here."""
    coord = _RecordingCoordinator()
    runner = HuntRunner(coord, _CountingWorker(), {}, cap=8)

    runner.tick(1000)

    assert "backlog_count" not in coord.calls


def test_a_failed_backlog_read_costs_the_gauge_not_the_runs(manager):
    coord = _RecordingCoordinator(backlog_raises=True)
    worker = _CountingWorker()
    runner, now = _due_runner(coord, worker, metrics=HuntRunnerMetrics(manager))

    assert runner.tick(now) == 1
    assert worker.runs == 1
    assert manager.observed("hunt_backlog") == []


def test_a_claimed_fire_counts_a_won_claim_a_success_and_a_tick(manager):
    coord = _WatermarkCoordinator()
    runner, now = _due_runner(coord, _CountingWorker(), metrics=HuntRunnerMetrics(manager))

    assert runner.tick(now) == 1

    assert [o.labels for o in manager.observed("hunt_claims_total")] == [
        {"hunt_id": "h", "outcome": "won"}
    ]
    assert [o.labels["outcome"] for o in manager.observed("hunt_runs_total")] == ["success"]
    assert len(manager.observed("hunt_run_duration_seconds")) == 1
    assert len(manager.observed("hunt_tick_duration_seconds")) == 1


def test_a_failed_fire_counts_a_failure_and_logs_the_error(manager):
    coord = _RecordingCoordinator()
    runner, now = _due_runner(coord, _FailingWorker(), metrics=HuntRunnerMetrics(manager))
    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        assert runner.tick(now) == 0
    finally:
        logger.remove(handler_id)

    lines = [m for m in captured if "hunt fire failed" in m]
    assert len(lines) == 1
    extra = lines[0].record["extra"]
    assert extra["hunt_id"] == "h"
    assert "clickhouse rejected the insert" in extra["error"]
    assert [o.labels["outcome"] for o in manager.observed("hunt_runs_total")] == ["failure"]
    # The lease is still released, so the next tick can retry the fire.
    assert coord.calls.count("release") == 1


def test_a_deferred_fire_counts_an_overrun(manager):
    coord = _LeasedCoordinator()
    runner, now = _due_runner(coord, _CountingWorker(), metrics=HuntRunnerMetrics(manager))

    assert runner.tick(now) == 0

    assert manager.observed("hunt_overruns_total") == [
        Observation("hunt_overruns_total", {"hunt_id": "h"}, "inc", 1)
    ]


def test_a_lost_claim_is_recorded_as_lease_churn(manager):
    coord = _LosingCoordinator()
    runner, now = _due_runner(coord, _CountingWorker(), metrics=HuntRunnerMetrics(manager))

    assert runner.tick(now) == 0

    assert [o.labels for o in manager.observed("hunt_claims_total")] == [
        {"hunt_id": "h", "outcome": "lost"}
    ]
    assert manager.observed("hunt_runs_total") == []


def test_a_claim_won_on_a_fire_a_peer_already_ran_is_released_unrun(manager):
    coord = _PeerRanItCoordinator()
    worker = _CountingWorker()
    runner, now = _due_runner(coord, worker, metrics=HuntRunnerMetrics(manager))

    assert runner.tick(now) == 0

    assert worker.runs == 0
    assert coord.calls.count("release") == 1
    assert [o.labels for o in manager.observed("hunt_claims_total")] == [
        {"hunt_id": "h", "outcome": "already_run"}
    ]
    assert manager.observed("hunt_runs_total") == []
