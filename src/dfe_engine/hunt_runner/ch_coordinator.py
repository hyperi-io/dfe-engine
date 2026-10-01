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
(insert-and-resolve): insert a claim, wait a short settle window, re-read, and the
latest claim (max claimed, then min owner) is the winner. The latest claim is also
the row a merge keeps, since hunt_lease collapses to one row per hunt, so the answer
does not change when parts merge. The claim INSERT itself checks the hunt's newest
lease row and inserts nothing when another owner's is live, so a worker that read
the hunt as free cannot then steal it from one that claimed it in the meantime.

A single worker (the dfe-docker case and most deploys) is exactly-once. With many
workers, two claims can still both land if their conditional inserts run at the
same moment on the server; the settle window gives both the same winner as long as
both inserts commit inside it. A duplicate run is NOT absorbed: the detection table
is a plain MergeTree, so the same window inserted twice writes its rows twice. The
watermark gives crash-safe incremental resume; hunt_state carries the
too_aggressive / overrun signal for the UI.

Tables (all ReplacingMergeTree; the first three bounded to ~1 row/hunt after merge):
  hunt_lease     - who holds a hunt now, until when (mutual exclusion + cap input)
  hunt_watermark - last committed window end per hunt (incremental resume)
  hunt_state     - overrun_count / too_aggressive (UI signal)
  hunt_run       - one row per FIRE: an operator's run-now request, then what that
                   run wrote. Keyed by (hunt_id, fire), so it is the run history
                   the API reads rather than a single current row.
  hunt_runner_heartbeat - one row per RUNNER: the last tick it started. A lease is
                   held only while a hunt executes, so this is the only thing that
                   says an idle runner is alive.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.schema.require import require_objects

from .schedule import due_count

# The coordination objects this worker reads and writes, by manifest id. The
# engine's schema phase creates them; nothing here does.
_COORDINATION_IDS = (
    "data.hunt_lease",
    "data.hunt_watermark",
    "data.hunt_state",
    "data.hunt_schedule",
    "data.hunt_run",
    "data.hunt_runner_heartbeat",
)


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
        worker_id: this worker's stable id, used as the lease owner.
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
        worker_id: str = "worker-0",
        lease_seconds: int = 300,
        settle_seconds: float = 0.75,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not database:
            raise ValueError("ChCoordinator requires an explicit data database")
        self._ch = ch
        self._db = database
        self._qdb = quote_identifier(database)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._settle = settle_seconds
        self._clock = clock
        self._sleep = sleep

    @property
    def worker_id(self) -> str:
        """This worker's lease-owner id."""
        return self._worker_id

    def now(self) -> int:
        """Current epoch seconds (int) from the injected clock."""
        return int(self._clock())

    # ---- schema -------------------------------------------------------

    def ensure_schema(self) -> None:
        """Assert the coordination tables exist. The engine's schema phase makes them.

        A read, not an apply. This worker is a separate pod and used to create the
        same tables through its own applier, which races an engine replica on
        ``ALTER TABLE ADD COLUMN`` and is a second answer to when an object comes
        into existence. Absent means the phase has not converged here, and that is
        worth failing loudly rather than papering over with a create.
        """
        require_objects(
            self._ch,
            database=self._db,
            object_ids=_COORDINATION_IDS,
            what="hunt-runner coordination",
        )

    # ---- lease (claim) ------------------------------------------------

    def current_lease(self, hunt_id: str) -> Lease | None:
        """The current lease = the deterministic latest claim for the hunt.

        Ordered by (claimed DESC, owner ASC) so two concurrent readers agree on the
        single winner. It is also the row a merge keeps, so the answer holds whatever
        the table's merge state.
        """
        rows = self._ch.query(
            f"SELECT owner, fire, lease_until FROM {self._qdb}.hunt_lease "  # noqa: S608 - quoted configured database; values bound
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

    def _insert_claim_if_free(self, hunt_id: str, fire: int, now: int) -> None:
        """Insert this worker's claim unless another owner's newest lease row is live.

        The check and the insert are one statement, so the hunt is re-read at the
        moment the claim would land rather than when this worker last looked.
        """
        self._ch.command(
            f"INSERT INTO {self._qdb}.hunt_lease (hunt_id, owner, fire, lease_until) "  # noqa: S608 - quoted configured database; values bound
            "SELECT {h:String}, {o:String}, {f:Int64}, {u:Int64} "
            "WHERE (SELECT count() FROM ("
            f"SELECT owner, lease_until FROM {self._qdb}.hunt_lease "
            "WHERE hunt_id = {h:String} ORDER BY claimed DESC, owner ASC LIMIT 1"
            ") WHERE lease_until > {now:Int64} AND owner != {o:String}) = 0",
            parameters={
                "h": hunt_id,
                "o": self._worker_id,
                "f": fire,
                "u": now + self._lease_seconds,
                "now": now,
            },
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
        self._insert_claim_if_free(hunt_id, fire, now)
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
            "SELECT countIf(lu > {now:Int64}) FROM ("  # noqa: S608 - quoted configured database; values bound
            f"SELECT hunt_id, argMax(lease_until, claimed) AS lu "
            f"FROM {self._qdb}.hunt_lease GROUP BY hunt_id)",
            parameters={"now": now},
        ).result_rows
        return int(rows[0][0]) if rows else 0

    def backlog_count(self) -> int:
        """Hunts due and unclaimed right now - the same SQL the KEDA shim reads.

        One due-count source of truth: the runner's backlog gauge and the shim's
        scaler answer cannot disagree, because both run ``schedule.due_query``.
        """
        return due_count(self._ch, self._db)

    # ---- watermark (incremental resume) -------------------------------

    def get_watermark(self, hunt_id: str) -> int | None:
        """Last committed window end for the hunt, or None if never run."""
        rows = self._ch.query(
            f"SELECT watermark FROM {self._qdb}.hunt_watermark "  # noqa: S608 - quoted configured database; values bound
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
            f"SELECT overrun_count, too_aggressive FROM {self._qdb}.hunt_state "  # noqa: S608 - quoted configured database; values bound
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

    # ---- runs (row count + operator run-now) --------------------------

    def record_run(self, hunt_id: str, fire: int, rows_written: int) -> None:
        """Record what a completed fire wrote (replaces any request for that fire)."""
        self._ch.insert(
            "hunt_run",
            [[hunt_id, fire, "completed", int(rows_written)]],
            column_names=["hunt_id", "fire", "status", "rows_written"],
            database=self._db,
        )

    def request_run(self, hunt_id: str, fire: int) -> None:
        """Mark a hunt due at *fire*, for the running runner to claim on its next poll.

        This is the whole of run-now: no push, no listener. The runner reads these
        with the same never-double-run machinery it uses for a scheduled fire.
        """
        self._ch.insert(
            "hunt_run",
            [[hunt_id, fire, "requested", 0]],
            column_names=["hunt_id", "fire", "status", "rows_written"],
            database=self._db,
        )

    def pending_runs(self, now: int | None = None) -> dict[str, int]:
        """Every hunt with an outstanding run-now request, as {hunt_id: fire}.

        ONE query per tick rather than one per hunt: the runner asks once and looks
        the answer up per spec. A request whose fire the watermark has passed is
        already served, so it is filtered here rather than re-run.
        """
        now = self.now() if now is None else now
        rows = self._ch.query(
            "SELECT r.hunt_id, r.fire FROM ("  # noqa: S608 - quoted configured database; values bound
            "SELECT hunt_id, fire FROM ("
            "SELECT hunt_id, fire, argMax(status, updated) AS status "
            f"FROM {self._qdb}.hunt_run GROUP BY hunt_id, fire) WHERE status = 'requested'"
            ") r LEFT JOIN (SELECT hunt_id, argMax(watermark, updated) AS wm "
            f"FROM {self._qdb}.hunt_watermark GROUP BY hunt_id) w USING (hunt_id) "
            "WHERE r.fire <= {now:Int64} AND coalesce(w.wm, 0) < r.fire",
            parameters={"now": now},
        ).result_rows
        return {str(row[0]): int(row[1]) for row in rows}

    # ---- heartbeat (runner liveness) ----------------------------------

    def heartbeat(self, now: int, poll_seconds: float) -> None:
        """Record that this runner ticked at *now*, on a *poll_seconds* cadence."""
        self._ch.insert(
            "hunt_runner_heartbeat",
            [[self._worker_id, int(now), float(poll_seconds)]],
            column_names=["runner_id", "seen", "poll_seconds"],
            database=self._db,
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
