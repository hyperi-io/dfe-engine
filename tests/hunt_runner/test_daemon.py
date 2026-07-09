#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_daemon.py
#  Purpose:      Tests for the pure daemon loop - cadence, reload, prompt stop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The daemon loop is a pure higher-order function, so it is tested with REAL
callback functions and an injected clock/sleep - no ClickHouse, no mocks. Each
test wires plain closures (a list-appending tick, a counting reload, a
list/iterator clock) and asserts on the observable effects: tick count, the exact
epoch ints tick saw, when sleep was and was not called, and the reload cadence."""

from __future__ import annotations

from collections.abc import Iterator

from dfe_engine.hunt_runner.daemon import run_loop


def _clock_from(values: list[int]) -> tuple:
    """A clock closure that yields the given ints in order, plus the seen-list.

    Returned clock returns float (as a real time.monotonic would); the loop int()s
    it, so we recover the exact epoch ints tick was handed.
    """
    it: Iterator[int] = iter(values)
    return (lambda: float(next(it)), values)


def test_runs_exactly_n_ticks_then_stops():
    # tick appends the epoch it saw; stop once N are recorded.
    epochs = [100, 200, 300, 400, 500]
    clock, _ = _clock_from(epochs)
    seen: list[int] = []

    def tick(now: int) -> None:
        seen.append(now)

    def should_stop() -> bool:
        return len(seen) >= 3

    ticks = run_loop(
        tick=tick,
        should_stop=should_stop,
        clock=clock,
        sleep=lambda _s: None,
        poll_seconds=5.0,
    )

    assert ticks == 3
    assert seen == [100, 200, 300]  # exact injected clock sequence, in order


def test_sleep_not_called_after_final_tick_and_uses_poll_seconds():
    # sleep records every duration it is asked for; a prompt stop means the tick
    # that trips should_stop is NOT followed by a sleep.
    clock, _ = _clock_from([1, 2, 3])
    seen: list[int] = []
    slept: list[float] = []

    def tick(now: int) -> None:
        seen.append(now)

    ticks = run_loop(
        tick=tick,
        should_stop=lambda: len(seen) >= 2,
        clock=clock,
        sleep=lambda s: slept.append(s),
        poll_seconds=7.5,
    )

    # 2 ticks; sleep ran only after the first (1 fewer than ticks) - none after the
    # final tick, and always with poll_seconds.
    assert ticks == 2
    assert slept == [7.5]


def test_reload_fires_at_positive_multiples_only():
    # 7 ticks, reload_every=3 -> reload before ticks 3 and 6 (0-based counter is a
    # positive multiple), never at tick 0. So exactly 2 reloads.
    clock, _ = _clock_from(list(range(7)))
    seen: list[int] = []
    reloads: list[int] = []

    def tick(now: int) -> None:
        seen.append(now)

    run_loop(
        tick=tick,
        should_stop=lambda: len(seen) >= 7,
        clock=clock,
        sleep=lambda _s: None,
        poll_seconds=1.0,
        on_reload=lambda: reloads.append(len(seen)),
        reload_every=3,
    )

    # reload runs BEFORE the tick, so len(seen) at reload time is the count already
    # done: 3 (before the 4th tick) and 6 (before the 7th tick).
    assert reloads == [3, 6]


def test_reload_disabled_when_every_zero():
    clock, _ = _clock_from(list(range(5)))
    seen: list[int] = []
    reloads: list[int] = []

    run_loop(
        tick=lambda now: seen.append(now),
        should_stop=lambda: len(seen) >= 5,
        clock=clock,
        sleep=lambda _s: None,
        poll_seconds=1.0,
        on_reload=lambda: reloads.append(1),
        reload_every=0,  # disabled -> never reloads even with a callback set
    )

    assert reloads == []


def test_reload_not_called_when_callback_none():
    clock, _ = _clock_from(list(range(5)))
    seen: list[int] = []

    # reload_every set but no callback -> the guard on on_reload keeps it a no-op.
    ticks = run_loop(
        tick=lambda now: seen.append(now),
        should_stop=lambda: len(seen) >= 5,
        clock=clock,
        sleep=lambda _s: None,
        poll_seconds=1.0,
        on_reload=None,
        reload_every=2,
    )

    assert ticks == 5  # loop still runs; reload simply never fires


def test_tick_sees_exact_epoch_ints_from_deterministic_clock():
    # An increasing, non-uniform clock sequence -> tick must see those exact ints,
    # proving the loop int()s clock() fresh each cycle in order.
    epochs = [1_000, 1_003, 1_009, 1_050, 2_000, 2_001]
    clock, _ = _clock_from(epochs)
    seen: list[int] = []

    ticks = run_loop(
        tick=lambda now: seen.append(now),
        should_stop=lambda: len(seen) >= len(epochs),
        clock=clock,
        sleep=lambda _s: None,
        poll_seconds=0.0,
    )

    assert ticks == len(epochs)
    assert seen == epochs


def test_stops_immediately_when_should_stop_true_at_start():
    # Already-stopping before the first tick -> zero ticks, tick never called.
    seen: list[int] = []

    ticks = run_loop(
        tick=lambda now: seen.append(now),
        should_stop=lambda: True,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
        poll_seconds=1.0,
    )

    assert ticks == 0
    assert seen == []
