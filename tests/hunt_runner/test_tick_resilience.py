#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_tick_resilience.py
#  Purpose:      Tick survives one hunt's failure + overrun gating semantics
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Failure isolation + overrun gating for ``HuntRunner.tick``.

Two invariants the multi-pod runner leans on, tested with an in-memory
coordinator (the CH-backed behaviour itself is covered by the live-CH
integration suite in tests/integration/test_ch_coordinator.py):

  1. one hunt's failure (bad SQL in its YAML, transient CH error) must not kill
     the tick - the remaining due hunts still run, the failed hunt's lease is
     released (not stuck claimed), and the loop survives to the next tick;
  2. an overrun is only an overrun when a PRIOR fire is still running as the
     next one comes due - a lease for the CURRENT fire is healthy multi-pod
     operation, and the same (hunt, fire) overrun is recorded once, not once
     per tick.
"""

from __future__ import annotations

import pytest

from dfe_engine.hunt_runner.ch_coordinator import Lease
from dfe_engine.hunt_runner.models import HuntSpec
from dfe_engine.hunt_runner.runner import HuntRunner
from dfe_engine.hunt_runner.spread import current_fire

# offset window = interval * 0.8 = 480 < 599, so EVERY hunt id is due at
# boundary + 599 regardless of its hash-derived phase offset.
INTERVAL = 600
NOW = INTERVAL + 599


def _spec(hunt_id: str) -> HuntSpec:
    return HuntSpec(hunt_id=hunt_id, interval_seconds=INTERVAL, query="INSERT INTO t {window}")


class FakeCoordinator:
    """In-memory ChCoordinator stand-in with the same tick-facing semantics."""

    def __init__(self) -> None:
        self.leases: dict[str, Lease] = {}
        self.watermarks: dict[str, int] = {}
        self.overruns: list[str] = []
        self.released: list[tuple[str, int]] = []
        self.owner = "me"

    def active_count(self, now: int) -> int:
        return sum(1 for lease in self.leases.values() if lease.lease_until > now)

    def get_watermark(self, hunt_id: str) -> int | None:
        return self.watermarks.get(hunt_id)

    def set_watermark(self, hunt_id: str, watermark: int) -> None:
        self.watermarks[hunt_id] = watermark

    def current_lease(self, hunt_id: str) -> Lease | None:
        return self.leases.get(hunt_id)

    def try_claim(self, hunt_id: str, fire: int, now: int) -> bool:
        lease = self.leases.get(hunt_id)
        if lease is not None and lease.lease_until > now and lease.owner != self.owner:
            return False
        self.leases[hunt_id] = Lease(
            hunt_id=hunt_id, owner=self.owner, fire=fire, lease_until=now + 300
        )
        return True

    def release(self, hunt_id: str, fire: int) -> None:
        self.released.append((hunt_id, fire))
        self.leases[hunt_id] = Lease(hunt_id=hunt_id, owner=self.owner, fire=fire, lease_until=0)

    def record_overrun(self, hunt_id: str) -> None:
        self.overruns.append(hunt_id)


class FlakyWorker:
    """Worker that raises for the configured hunts and records the rest."""

    def __init__(self, coord: FakeCoordinator, fail: set[str] | None = None) -> None:
        self._coord = coord
        self._fail = fail or set()
        self.ran: list[tuple[str, int]] = []

    def run(self, spec: HuntSpec, scheduled_start: int) -> int:
        if spec.hunt_id in self._fail:
            raise RuntimeError(f"boom: {spec.hunt_id}")
        self.ran.append((spec.hunt_id, scheduled_start))
        self._coord.set_watermark(spec.hunt_id, scheduled_start)
        return scheduled_start


# ---- failure isolation (one hunt must not kill the worker) -------------


def test_one_hunt_failure_does_not_kill_the_tick():
    coord = FakeCoordinator()
    worker = FlakyWorker(coord, fail={"a"})
    runner = HuntRunner(coord, worker, {"a": _spec("a"), "b": _spec("b")}, cap=8)

    runner.tick(NOW)  # hunt a raises - the tick must not

    # hunt b still ran in the SAME tick, after a's failure
    assert [h for h, _ in worker.ran] == ["b"]
    # a's lease was released (finally) - not stuck claimed until lease expiry
    assert {h for h, _ in coord.released} == {"a", "b"}
    # a's watermark did NOT advance, so the failed fire retries
    assert coord.get_watermark("a") is None

    # and the loop survives to the next tick (b's next fire runs)
    runner.tick(NOW + INTERVAL)
    assert [h for h, _ in worker.ran] == ["b", "b"]


def test_loop_machinery_crash_still_propagates():
    class BrokenCoordinator(FakeCoordinator):
        def active_count(self, now: int) -> int:
            raise RuntimeError("CH down")

    coord = BrokenCoordinator()
    runner = HuntRunner(coord, FlakyWorker(coord), {"a": _spec("a")}, cap=8)
    with pytest.raises(RuntimeError, match="CH down"):
        runner.tick(NOW)


# ---- overrun gating (prior fire only, once per fire) --------------------


def test_current_fire_lease_records_no_overrun():
    coord = FakeCoordinator()
    worker = FlakyWorker(coord)
    runner = HuntRunner(coord, worker, {"a": _spec("a")}, cap=8)

    fire = current_fire("a", INTERVAL, NOW)
    # another pod is running THIS fire right now - healthy multi-pod operation
    coord.leases["a"] = Lease(hunt_id="a", owner="other", fire=fire, lease_until=NOW + 300)

    runner.tick(NOW)

    assert coord.overruns == []  # not an overrun
    assert worker.ran == []  # deferred - never double-run


def test_prior_fire_lease_records_overrun_once_per_fire():
    coord = FakeCoordinator()
    worker = FlakyWorker(coord)
    runner = HuntRunner(coord, worker, {"a": _spec("a")}, cap=8)

    fire = current_fire("a", INTERVAL, NOW)
    # the PREVIOUS fire is still running as this one comes due -> overrun
    coord.leases["a"] = Lease(
        hunt_id="a", owner="other", fire=fire - INTERVAL, lease_until=NOW + 10_000
    )

    runner.tick(NOW)
    runner.tick(NOW)  # same due fire again next poll

    assert coord.overruns == ["a"]  # once per (hunt, fire), not once per tick
    assert worker.ran == []  # still deferred - never double-run

    # the NEXT fire coming due against the still-running lease is a new overrun
    runner.tick(NOW + INTERVAL)
    assert coord.overruns == ["a", "a"]
