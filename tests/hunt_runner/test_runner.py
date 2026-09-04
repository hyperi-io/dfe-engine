#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_runner.py
#  Purpose:      Tests for hunt-runner load-spread, double-run decision, heartbeat
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deterministic spread + scheduling-decision tests, and the tick's heartbeat."""

from __future__ import annotations

from dfe_engine.hunt_runner import HuntState, decide, next_due, phase_offset
from dfe_engine.hunt_runner.runner import HuntRunner
from dfe_engine.hunt_runner.scheduler import mark_deferred


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


def test_due_now_and_current_fire():
    from dfe_engine.hunt_runner.spread import current_fire, due_now

    # fire = boundary + stable offset; due once now passes it
    fire = current_fire("h", 600, 1000)
    assert 600 <= fire < 1200
    assert due_now("h", 600, fire) is True
    assert due_now("h", 600, fire - 1) is False


def test_mark_deferred_flags_too_aggressive():
    st = HuntState(hunt_id="h", status="running", overrun_count=1)
    out = mark_deferred(st)
    assert out.too_aggressive is True
    assert out.overrun_count == 2
    assert out.status == "deferred"


class _RecordingCoordinator:
    """A coordinator that records call order and grants every claim."""

    def __init__(self, *, heartbeat_raises: bool = False) -> None:
        self.calls: list[str] = []
        self.beats: list[tuple[int, float]] = []
        self._heartbeat_raises = heartbeat_raises

    def heartbeat(self, now: int, poll_seconds: float) -> None:
        self.calls.append("heartbeat")
        if self._heartbeat_raises:
            raise RuntimeError("clickhouse is down")
        self.beats.append((now, poll_seconds))

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


class _CountingWorker:
    """A worker that runs nothing and only counts."""

    def __init__(self) -> None:
        self.runs = 0

    def run(self, spec, fire: int) -> None:
        self.runs += 1


def _due_runner(coord, worker, *, poll_seconds: float = 15.0) -> tuple[HuntRunner, int]:
    """A runner holding one hunt, plus a *now* at which that hunt is due."""
    from dfe_engine.hunt_runner.models import HuntSpec

    spec = HuntSpec(hunt_id="h", interval_seconds=600, queries=["SELECT 1"])
    # The fire is the interval boundary plus the hunt's stable offset, so a now on
    # that instant is due without any dependence on the real clock.
    now = 600_000 + phase_offset("h", 600)
    runner = HuntRunner(coord, worker, {"h": spec}, cap=8, poll_seconds=poll_seconds)
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
