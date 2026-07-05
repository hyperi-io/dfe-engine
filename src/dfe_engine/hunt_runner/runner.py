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
    ) -> None:
        self._coord = coordinator
        self._worker = worker
        self._specs = specs
        self._cap = cap
        # Last fire we recorded an overrun for, per hunt - the too_aggressive
        # signal is once per (hunt, fire), not once per poll tick. In-memory is
        # enough: a rebuild (spec reload) re-recording a rare overrun only nudges
        # a soft UI counter.
        self._overrun_recorded: dict[str, int] = {}

    def tick(self, now: int) -> int:
        """One cycle: claim + run every due hunt (never double-run), up to the cap.

        Returns the number of runs executed this tick.
        """
        running = self._coord.active_count(now)
        executed = 0
        for spec in self._specs.values():
            if running + executed >= self._cap:
                break  # global cap protects ClickHouse; lateness surfaces as overload
            if not due_now(spec.hunt_id, spec.interval_seconds, now):
                continue
            fire = current_fire(spec.hunt_id, spec.interval_seconds, now)
            watermark = self._coord.get_watermark(spec.hunt_id)
            if watermark is not None and watermark >= fire:
                continue  # this fire already completed - idempotent across ticks
            lease = self._coord.current_lease(spec.hunt_id)
            if lease is not None and lease.lease_until > now:
                # Someone is running this hunt. Only a lease from a PRIOR fire
                # means the hunt overruns its interval (too_aggressive); a lease
                # for the CURRENT fire is healthy multi-pod operation. Record
                # once per (hunt, fire), not once per poll tick.
                if lease.fire < fire and self._overrun_recorded.get(spec.hunt_id) != fire:
                    self._coord.record_overrun(spec.hunt_id)
                    self._overrun_recorded[spec.hunt_id] = fire
                continue  # defer, NEVER double-run
            if not self._coord.try_claim(spec.hunt_id, fire, now):
                continue  # lost the settle-window race -> another worker has it
            try:
                self._worker.run(spec, fire)
            except Exception:
                # One hunt's failure (bad SQL in its YAML, transient CH error)
                # must not kill the worker: log, release the lease (finally),
                # keep going. The watermark did NOT advance, so this fire
                # retries on a later tick. A crash of the loop machinery itself
                # (the coordinator calls above) still propagates.
                logger.exception(
                    "hunt {} fire {} failed; lease released, continuing", spec.hunt_id, fire
                )
            finally:
                self._coord.release(spec.hunt_id, fire)
            executed += 1
        return executed
