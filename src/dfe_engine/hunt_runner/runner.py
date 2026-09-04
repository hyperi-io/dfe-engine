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
"""

from __future__ import annotations

from scalo.logger import logger

from .ch_coordinator import ChCoordinator
from .models import HuntSpec
from .spread import current_fire, due_now
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
    ) -> None:
        self._coord = coordinator
        self._worker = worker
        self._specs = specs
        self._cap = cap
        # Beaten into the heartbeat: a reader judges this runner against its own cadence.
        self._poll_seconds = poll_seconds

    def _fire_for(self, spec: HuntSpec, now: int, requested: dict[str, int]) -> int | None:
        """The fire this tick should run for the hunt, or None if there is nothing.

        The schedule first: this interval's fire, unless the watermark says it is
        already done. Failing that, an operator's run-now, which is just a fire the
        API wrote down. Both go through the same claim, so a requested run cannot
        double-run one already in flight.
        """
        if due_now(spec.hunt_id, spec.interval_seconds, now):
            fire = current_fire(spec.hunt_id, spec.interval_seconds, now)
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

    def tick(self, now: int) -> int:
        """One cycle: claim + run every due hunt (never double-run), up to the cap.

        The heartbeat goes first so a runner with nothing due still reads as alive.

        Returns the number of runs executed this tick.
        """
        self._beat(now)
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
                continue
            if not self._coord.try_claim(spec.hunt_id, fire, now):
                continue  # lost the settle-window race -> another worker has it
            try:
                self._worker.run(spec, fire)
                executed += 1
            except Exception as exc:
                # One hunt's failure must not end the tick: the others are still due,
                # and the daemon loop above this would exit on an escaping exception.
                logger.error(f"hunt {spec.hunt_id} failed at fire {fire}: {exc}")
            finally:
                self._coord.release(spec.hunt_id, fire)
        return executed
