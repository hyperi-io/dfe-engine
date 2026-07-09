#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_runner.py
#  Purpose:      Tests for hunt-runner load-spread + never-double-run decision
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deterministic spread + scheduling-decision tests."""

from __future__ import annotations

from dfe_engine.hunt_runner import HuntState, decide, next_due, phase_offset
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
