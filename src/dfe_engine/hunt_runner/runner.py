#  Project:      dfe-engine
#  File:         hunt_runner/runner.py
#  Purpose:      The pull-based runner tick: due -> claim -> execute (CH-only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One cycle of the multi-pod hunt runner, coordinated entirely through ClickHouse.

``tick`` is the unit a long-running worker repeats: for each due hunt that has not
already completed this fire and is not already running, claim it (insert-and-resolve
lease -> never double-run) and execute it, up to the global cap. Pull-based -> no
hunt is assigned to a pod; add a pod and it just competes for claims; kill one and
its lease expires and is reclaimed. The daemon loop is a thin ``while: tick(); sleep``
wrapper around this.

An operator's run-now arrives the same way: the API writes a fire into hunt_run and
the next tick picks it up. Nothing pushes at the runner, so a run-now needs no
listener and behaves like any other fire once claimed.

Every tick opens with a heartbeat, before the due/claim/execute work. That is what
tells the API a runner exists: an idle runner holds no lease, so without the beat a
healthy stack with nothing due is indistinguishable from no runner at all.

The tick also reports itself through :mod:`dfe_engine.hunt_runner.metrics` - the
backlog gauge, the claim outcome, and a counter plus a duration per fire - so a
runner that stops firing is visible in telemetry rather than only as an absence of
detection rows.
"""

from __future__ import annotations

import time

from scalo.logger import logger

from .ch_coordinator import ChCoordinator
from .metrics import HuntRunnerMetrics
from .models import HuntSpec
from .spread import latest_fire
from .worker import HuntWorker


class HuntRunner:
    """Ties the spread/cap decision to the ClickHouse coordinator + worker."""

    def __init__(
        self,
        coordinator: ChCoordinator,
        worker: HuntWorker,
        specs: dict[str, HuntSpec],
        cap: int = 8,
        poll_seconds: float = 15.0,
        metrics: HuntRunnerMetrics | None = None,
    ) -> None:
        self._coord = coordinator
        self._worker = worker
        self._specs = specs
        self._cap = cap
        # Beaten into the heartbeat: a reader judges this runner against its own cadence.
        self._poll_seconds = poll_seconds
        # No manager wired records nothing, which is what the unit suite runs on.
        self._metrics = metrics or HuntRunnerMetrics()

    def _fire_for(self, spec: HuntSpec, now: int, requested: dict[str, int]) -> int | None:
        """The fire this tick should run for the hunt, or None if there is nothing.

        The schedule first: the latest fire at or before now, unless the watermark
        says it is already done. Failing that, an operator's run-now, which is just
        a fire the API wrote down. Both go through the same claim, so a requested
        run cannot double-run one already in flight.
        """
        fire = latest_fire(spec.hunt_id, spec.interval_seconds, now)
        watermark = self._coord.get_watermark(spec.hunt_id)
        if watermark is None or watermark < fire:
            return fire
        return requested.get(spec.hunt_id)

    def _beat(self, now: int) -> None:
        """Say this runner is alive. A failed beat costs visibility; a lost tick costs runs."""
        try:
            self._coord.heartbeat(now, self._poll_seconds)
        except Exception as exc:
            logger.warning(f"hunt runner heartbeat failed at {now}: {exc}")

    def _report_backlog(self) -> None:
        """Publish the due-and-unclaimed count, off the SAME query KEDA scales on.

        Only when a backend is wired: it is another ClickHouse round trip per tick,
        so a deployment that reads no metrics does not pay for it. A failed read
        costs the gauge, not the tick's runs.
        """
        if not self._metrics.enabled:
            return
        try:
            self._metrics.backlog(self._coord.backlog_count())
        except Exception as exc:
            logger.warning("hunt backlog gauge unavailable", error=str(exc))

    def tick(self, now: int) -> int:
        """One cycle: claim + run every due hunt (never double-run), up to the cap.

        The heartbeat goes first so a runner with nothing due still reads as alive.

        Returns the number of runs executed this tick.
        """
        tick_began = time.monotonic()
        self._beat(now)
        self._report_backlog()
        running = self._coord.active_count(now)
        # Read every outstanding run-now once per tick, not once per hunt.
        requested = self._coord.pending_runs(now)
        executed = 0
        for spec in self._specs.values():
            if running + executed >= self._cap:
                break  # global cap protects ClickHouse; lateness surfaces as overload
            fire = self._fire_for(spec, now, requested)
            if fire is None:
                continue
            lease = self._coord.current_lease(spec.hunt_id)
            if lease is not None and lease.lease_until > now:
                # still running from a prior fire -> defer, NEVER double-run
                self._coord.record_overrun(spec.hunt_id)
                self._metrics.overrun(spec.hunt_id)
                continue
            claimed = self._coord.try_claim(spec.hunt_id, fire, now)
            self._metrics.claim(spec.hunt_id, won=claimed)
            if not claimed:
                continue  # lost the settle-window race -> another worker has it
            run_began = time.monotonic()
            try:
                self._worker.run(spec, fire)
                executed += 1
                self._metrics.run_completed(
                    spec.hunt_id, duration_seconds=time.monotonic() - run_began
                )
            except Exception as exc:
                # One hunt's failure must not end the tick: the others are still due,
                # and the daemon loop above this would exit on an escaping exception.
                self._metrics.run_failed(
                    spec.hunt_id, duration_seconds=time.monotonic() - run_began
                )
                logger.error(
                    "hunt fire failed",
                    hunt_id=spec.hunt_id,
                    fire=fire,
                    error=str(exc),
                )
            finally:
                self._coord.release(spec.hunt_id, fire)
        self._metrics.tick_completed(time.monotonic() - tick_began)
        return executed
