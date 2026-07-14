#  Project:      dfe-engine
#  File:         hunt_runner/daemon.py
#  Purpose:      The pure loop that repeats HuntRunner.tick until asked to stop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The daemon heartbeat: repeat one runner tick on a poll interval, until stopped.

``HuntRunner.tick(now)`` is one cycle (due -> claim -> execute against ClickHouse);
the long-running worker just repeats it on a poll interval, periodically reloading
hunt specs, until k8s sends SIGTERM. ``run_loop`` is that cadence as a PURE
higher-order function - every collaborator (the tick, the stop check, the clock,
the sleep, the reload) is injected. Keeping it pure means the real runner and its
ClickHouse stay entirely out of the loop's own tests: it is exercised with plain
callback functions, no mocks, no live services. The CLI wires the real runner, a
SIGTERM-driven stop flag, and "reload specs + rebuild the runner" into it, so the
loop itself stays agnostic about what a tick or a reload actually does.
"""

from __future__ import annotations

from collections.abc import Callable

from scalo.logger import logger


def run_loop(
    *,
    tick: Callable[[int], object],
    should_stop: Callable[[], bool],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    poll_seconds: float,
    on_reload: Callable[[], None] | None = None,
    reload_every: int = 0,
) -> int:
    """Repeat ``tick`` on the poll interval until ``should_stop`` is true.

    Each cycle, in order:
      - if ``on_reload`` is set and ``reload_every > 0`` and the count of ticks so
        far is a positive multiple of ``reload_every``, call ``on_reload`` FIRST
        (the CLI wires this to "reload hunt specs and rebuild the runner"; the loop
        stays agnostic). Never fires at count 0, so the first tick is not a reload.
      - call ``tick(int(clock()))`` and increment the tick counter;
      - re-check ``should_stop`` AFTER the tick and BEFORE sleeping, so a stop
        signalled during (or just before) a tick is prompt - no wasted final sleep;
      - otherwise ``sleep(poll_seconds)`` before the next cycle.

    Returns the total number of ticks executed. Injecting clock/sleep/stop keeps
    this pure and deterministic to test; the caller owns the real wall clock, the
    real ``time.sleep``, and the signal handler that flips the stop flag.
    """
    ticks = 0
    while not should_stop():
        if on_reload is not None and reload_every > 0 and ticks > 0 and ticks % reload_every == 0:
            on_reload()
        tick(int(clock()))
        ticks += 1
        if should_stop():
            break
        sleep(poll_seconds)
    logger.info(f"hunt-runner loop exiting after {ticks} tick(s)")
    return ticks
