#  Project:      dfe-engine
#  File:         hunt_runner/runner.py
#  Purpose:      The pull-based runner tick: enqueue due -> claim -> execute
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One cycle of the multi-pod hunt runner.

``tick`` is the unit a long-running worker repeats: enqueue any due hunts not
already active, claim up to the global cap via SKIP LOCKED, and execute each
claimed run. Pull-based -> no hunt is assigned to a pod; add a pod and it just
pulls. The daemon loop is a thin `while: tick(); sleep` wrapper around this.
"""

from __future__ import annotations

from .claim_table import ClaimTable
from .models import HuntSpec
from .spread import current_fire, due_now
from .worker import HuntWorker


class HuntRunner:
    """Ties the scheduler decision (spread/cap) to the claim table + worker."""

    def __init__(
        self,
        claims: ClaimTable,
        worker: HuntWorker,
        specs: dict[str, HuntSpec],
        cap: int = 8,
        worker_id: str = "worker-0",
    ) -> None:
        self._claims = claims
        self._worker = worker
        self._specs = specs
        self._cap = cap
        self._worker_id = worker_id

    def enqueue_due(self, now: int) -> int:
        """Enqueue this interval's fire for any due hunt not already active. Dedups."""
        active = self._claims.active_hunt_ids()
        n = 0
        for spec in self._specs.values():
            if spec.hunt_id in active:
                continue
            if due_now(spec.hunt_id, spec.interval_seconds, now):
                # due_at = the scheduled fire time = the query window's end
                self._claims.enqueue(
                    spec.hunt_id, current_fire(spec.hunt_id, spec.interval_seconds, now)
                )
                n += 1
        return n

    def drain(self, now: int) -> int:
        """Claim up to the global cap and execute each run at its scheduled window."""
        limit = max(0, self._cap - self._claims.running_count())
        if limit == 0:
            return 0
        claimed = self._claims.claim(self._worker_id, now, limit)
        for run in claimed:
            spec = self._specs.get(run["hunt_id"])
            if spec is not None:
                # window end = the run's scheduled fire time (due_at), so resume is exact
                self._worker.run(spec, run["due_at"])
            self._claims.complete(run["id"])
        return len(claimed)

    def tick(self, now: int) -> int:
        """One cycle: enqueue due hunts (deduped) then drain. Returns runs executed."""
        self.enqueue_due(now)
        return self.drain(now)
