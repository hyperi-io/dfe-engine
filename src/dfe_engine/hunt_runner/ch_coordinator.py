#  Project:      dfe-engine
#  File:         hunt_runner/ch_coordinator.py
#  Purpose:      ClickHouse-backed hunt coordination (lease + watermark + state)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse coordination for the hunt runner - the ONLY operational store.

ARCHITECTURE principle: ClickHouse is DFE's single operational store, so the hunt
runner keeps its transient coordination state in three small tables in the data
database (settings.clickhouse.effective_data_database), NOT in Postgres. That
removes the PG dependency which would otherwise make hunts mission-critical on a
convenience store, and keeps the runner working on a non-k8s single deploy
(dfe-docker) where the only thing guaranteed present is ClickHouse.

CH has no row locks / SELECT ... FOR UPDATE, so the per-hunt claim is optimistic
(insert-and-resolve): read the current lease, insert a claim, wait a short settle
window, re-read, and the deterministic latest claim (max claimed, then min owner)
is the winner. One active lease per hunt -> never-double-run. A single worker (the
dfe-docker case and most deploys) is exactly-once; with many workers a claim can
rarely race inside the settle window, giving a rare duplicate run that the
idempotent windowed INSERT absorbs (the same window re-inserted dedupes on the
target). The watermark gives crash-safe incremental resume; hunt_state carries the
too_aggressive / overrun signal for the UI.

Tables (all ReplacingMergeTree, bounded to ~1 row/hunt after merge):
  hunt_lease     - who holds a hunt now, until when (mutual exclusion + cap input)
  hunt_watermark - last committed window end per hunt (incremental resume)
  hunt_state     - overrun_count / too_aggressive (UI signal)
"""

from __future__ import annotations

import itertools
import os
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Distinguishes coordinators built in the SAME process (tests, embedded use);
# hostname+pid already separates pods.
_WORKER_SEQ = itertools.count()


def default_worker_id() -> str:
    """A lease-owner id unique per pod/process (and per coordinator instance).

    Every claimant MUST have a distinct owner: the insert-and-resolve settle
    resolves the winner as (latest claimed, then min owner), so two claimants
    sharing an id would BOTH resolve as the winner -> double-run. hostname+pid
    separates pods; the counter separates instances within one process.
    """
    return f"{socket.gethostname()}-{os.getpid()}-{next(_WORKER_SEQ)}"


@dataclass(frozen=True, slots=True)
class Lease:
    """The current lease on a hunt (the deterministic latest claim wins)."""

    hunt_id: str
    owner: str
    fire: int
    lease_until: int


@dataclass(frozen=True, slots=True)
class HuntStateRow:
    """Persisted UI signal for a hunt (overrun / too-aggressive)."""

    hunt_id: str
    overrun_count: int
    too_aggressive: bool


class ChCoordinator:
    """Hunt coordination in ClickHouse: lease (claim), watermark, and state.

    Args:
        ch: a clickhouse-connect client.
        database: the data database (settings.clickhouse.effective_data_database).
            NEVER hardcode 'dfe' here - the caller passes the resolved name.
        worker_id: this worker's lease-owner id. Defaults to a derived
            hostname-pid-seq id so every pod/process is a distinct claimant
            (a SHARED id breaks the settle's never-double-run guarantee).
        lease_seconds: how long a claim is held before it becomes reclaimable.
        settle_seconds: the insert-and-resolve settle window.
        clock: injectable epoch-seconds source (defaults to time.time).
        sleep: injectable sleep for the settle window (defaults to time.sleep).
    """

    def __init__(
        self,
        ch: Any,
        database: str,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        settle_seconds: float = 0.75,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not database:
            raise ValueError("ChCoordinator requires an explicit data database")
        self._ch = ch
        self._db = database
        self._worker_id = worker_id or default_worker_id()
        self._lease_seconds = lease_seconds
        self._settle = settle_seconds
        self._clock = clock
        self._sleep = sleep

    @property
    def worker_id(self) -> str:
        """This worker's lease-owner id."""
        return self._worker_id

    @property
    def lease_seconds(self) -> int:
        """Lease TTL. The worker heartbeats (renew) at a fraction of this so a
        run longer than the TTL is not reclaimed mid-flight (never-double-run)."""
        return self._lease_seconds

    def now(self) -> int:
        """Current epoch seconds (int) from the injected clock."""
        return int(self._clock())

    # ---- schema -------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the three coordination tables if absent (idempotent).

        Engines resolve through the topology-sensing resolver, so the tables get the
        right form on single (ReplacingMergeTree) / on-prem cluster
        (ReplicatedReplacingMergeTree ON CLUSTER) / Cloud (SharedReplacingMergeTree)
        - never a hardcoded literal. The database is created ON CLUSTER too where the
        topology needs it, else the ON CLUSTER table creates fail on the other
        replicas (the single -> cluster trap).
        """
        from ..clickhouse.engines import EngineResolver, EngineSpec

        resolver = EngineResolver(client=self._ch)
        lease = resolver.resolve(EngineSpec("ReplacingMergeTree", "claimed"), self._db)
        wmark = resolver.resolve(EngineSpec("ReplacingMergeTree", "updated"), self._db)
        state = resolver.resolve(EngineSpec("ReplacingMergeTree", "updated"), self._db)

        self._ch.command(f"CREATE DATABASE IF NOT EXISTS `{self._db}`{lease.on_cluster}")
        self._ch.command(
            f"CREATE TABLE IF NOT EXISTS `{self._db}`.hunt_lease{lease.on_cluster} ("
            "hunt_id String, owner String, fire Int64, lease_until Int64, "
            "claimed DateTime64(3) DEFAULT now64(3)) "
            f"ENGINE = {lease.clause} ORDER BY hunt_id"
        )
        self._ch.command(
            f"CREATE TABLE IF NOT EXISTS `{self._db}`.hunt_watermark{wmark.on_cluster} ("
            "hunt_id String, watermark Int64, updated DateTime64(3) DEFAULT now64(3)) "
            f"ENGINE = {wmark.clause} ORDER BY hunt_id"
        )
        self._ch.command(
            f"CREATE TABLE IF NOT EXISTS `{self._db}`.hunt_state{state.on_cluster} ("
            "hunt_id String, overrun_count Int64, too_aggressive UInt8, "
            "updated DateTime64(3) DEFAULT now64(3)) "
            f"ENGINE = {state.clause} ORDER BY hunt_id"
        )

    # ---- lease (claim) ------------------------------------------------

    def current_lease(self, hunt_id: str) -> Lease | None:
        """The current lease = the deterministic latest claim for the hunt.

        Ordered by (claimed DESC, owner ASC) so two concurrent readers agree on the
        single winner by scanning the (few) rows, without relying on merge state.
        """
        rows = self._ch.query(
            f"SELECT owner, fire, lease_until FROM `{self._db}`.hunt_lease "
            "WHERE hunt_id = {h:String} ORDER BY claimed DESC, owner ASC LIMIT 1",
            parameters={"h": hunt_id},
        ).result_rows
        if not rows:
            return None
        return Lease(
            hunt_id=hunt_id,
            owner=rows[0][0],
            fire=int(rows[0][1]),
            lease_until=int(rows[0][2]),
        )

    def _insert_lease(self, hunt_id: str, fire: int, lease_until: int) -> None:
        self._ch.insert(
            "hunt_lease",
            [[hunt_id, self._worker_id, fire, lease_until]],
            column_names=["hunt_id", "owner", "fire", "lease_until"],
            database=self._db,
        )

    def try_claim(self, hunt_id: str, fire: int, now: int | None = None) -> bool:
        """Claim a hunt for this worker (insert-and-resolve). True if we own it.

        Returns False if another worker holds an active lease (an overrun - the
        caller records it and does NOT run) or if we lost the settle-window race.
        """
        now = self.now() if now is None else now
        current = self.current_lease(hunt_id)
        if current is not None and current.lease_until > now and current.owner != self._worker_id:
            return False  # someone else is running this hunt -> never double-run
        self._insert_lease(hunt_id, fire, now + self._lease_seconds)
        self._sleep(self._settle)
        winner = self.current_lease(hunt_id)
        return winner is not None and winner.owner == self._worker_id

    def renew(self, hunt_id: str, fire: int, now: int | None = None) -> None:
        """Heartbeat a long-running claim so it is not reclaimed mid-run."""
        now = self.now() if now is None else now
        self._insert_lease(hunt_id, fire, now + self._lease_seconds)

    def release(self, hunt_id: str, fire: int) -> None:
        """Release our claim (lease_until=0) so the slot frees immediately."""
        self._insert_lease(hunt_id, fire, 0)

    def active_count(self, now: int | None = None) -> int:
        """Hunts with an active lease right now - the input to the global cap."""
        now = self.now() if now is None else now
        rows = self._ch.query(
            "SELECT countIf(lu > {now:Int64}) FROM ("
            f"SELECT hunt_id, argMax(lease_until, claimed) AS lu "
            f"FROM `{self._db}`.hunt_lease GROUP BY hunt_id)",
            parameters={"now": now},
        ).result_rows
        return int(rows[0][0]) if rows else 0

    # ---- watermark (incremental resume) -------------------------------

    def get_watermark(self, hunt_id: str) -> int | None:
        """Last committed window end for the hunt, or None if never run."""
        rows = self._ch.query(
            f"SELECT watermark FROM `{self._db}`.hunt_watermark "
            "WHERE hunt_id = {h:String} ORDER BY updated DESC LIMIT 1",
            parameters={"h": hunt_id},
        ).result_rows
        return int(rows[0][0]) if rows else None

    def set_watermark(self, hunt_id: str, watermark: int) -> None:
        """Advance the watermark (called only after the query has committed)."""
        self._ch.insert(
            "hunt_watermark",
            [[hunt_id, watermark]],
            column_names=["hunt_id", "watermark"],
            database=self._db,
        )

    # ---- state (UI overrun signal) ------------------------------------

    def get_state(self, hunt_id: str) -> HuntStateRow | None:
        """The hunt's persisted overrun / too-aggressive signal, if any."""
        rows = self._ch.query(
            f"SELECT overrun_count, too_aggressive FROM `{self._db}`.hunt_state "
            "WHERE hunt_id = {h:String} ORDER BY updated DESC LIMIT 1",
            parameters={"h": hunt_id},
        ).result_rows
        if not rows:
            return None
        return HuntStateRow(
            hunt_id=hunt_id,
            overrun_count=int(rows[0][0]),
            too_aggressive=bool(rows[0][1]),
        )

    def record_overrun(self, hunt_id: str) -> None:
        """Flag a hunt too-aggressive and bump its overrun count (UI signal).

        Read-then-insert (CH has no atomic increment); the count is a soft UI
        signal, so an approximate value under rare contention is acceptable.
        """
        current = self.get_state(hunt_id)
        count = (current.overrun_count if current else 0) + 1
        self._ch.insert(
            "hunt_state",
            [[hunt_id, count, 1]],
            column_names=["hunt_id", "overrun_count", "too_aggressive"],
            database=self._db,
        )
