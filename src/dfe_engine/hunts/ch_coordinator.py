#  Project:      dfe-engine
#  File:         hunts/ch_coordinator.py
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

CH has no row locks / SELECT ... FOR UPDATE, so the per-hunt claim follows the
shared lease discipline (:mod:`dfe_engine.clickhouse.lease`): read the hunt free,
insert a claim, wait a settle, and keep the claim only if it landed within that
settle and is still the latest row. Lease writes reach a majority of replicas and
lease reads go through ``FINAL`` on a replica holding them all, so every worker names
the same winner, before and after a merge. Exclusion has to hold, because no INSERT
... SELECT is deduplicated on 26.3 and a window run twice writes its rows twice.

hunt_lease keeps one row per hunt, so a later row can hide a holder. Each claim also
writes a marker under ``<hunt_id>#claim/<owner>``, which no other worker's row
collapses into, and a live marker blocks every other worker's claim.

A claim that misses has its marker ended and its row left in place, never released,
since that row can sit over a rival that stood. A row whose marker has ended holds
nothing, so the next poll by any worker can claim the hunt unless a rival's live
marker still holds it.

A claimed run is held under :meth:`ChCoordinator.held`, which renews the lease on its
own thread every third of the lease, so a run longer than the lease stays held while
its renewals land. A renewal lands only while this worker's marker for that fire
has more than a settle and a quorum wait left on the server's clock, and a release
frees the hunt only while this worker's row for that fire is the newest. So an owner
whose lease ran out cannot extend or free the lease a successor took.

The lease does not fence the run's own writes: a run whose renewals stop landing
keeps writing after a peer may have claimed the hunt. The renewals share the
runner's client, which must therefore not use a ClickHouse session.

The watermark gives crash-safe incremental resume, and hunt_state carries the
too_aggressive / overrun signal for the UI.

Tables (all ReplacingMergeTree, the first three bounded to ~1 row/hunt after merge):
  hunt_lease     - who holds a hunt now, until when (mutual exclusion + cap input),
                   plus one marker row per hunt and claiming worker
  hunt_watermark - last committed window end per hunt (incremental resume)
  hunt_state     - overrun_count / too_aggressive (UI signal)
  hunt_run       - one row per FIRE: an operator's run-now request, then what that
                   run wrote. Keyed by (hunt_id, fire), so it is the run history
                   the API reads rather than a single current row.
  hunt_runner_heartbeat - one row per RUNNER: the last tick it started. A lease is
                   held only while a hunt executes, so this is the only thing that
                   says an idle runner is alive.
"""

import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from dfe_engine.clickhouse.inserts import keep_identical_rows
from dfe_engine.clickhouse.lease import (
    QUORUM_TIMEOUT_MS,
    READ_SETTINGS,
    SETTLE_SECONDS,
    SlowClaimError,
    is_quorum_refusal,
    settle_claim,
    write_settings,
)
from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.hunts.schedule import due_count
from dfe_engine.schema.require import require_objects

_LEASE_COLUMNS = ["hunt_id", "owner", "fire", "lease_until"]

# A hunt id never holds "#", so a marker's key never names a hunt.
_MARKER = "#claim/"

# The coordination objects this worker reads and writes, by manifest id. The
# engine's schema phase creates them. Nothing here does.
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
        settle_seconds: how soon a claim must land after its free read, and how long
            it waits before the re-read.
        clock: injectable epoch-seconds source (defaults to time.time).
        sleep: injectable sleep for the settle window (defaults to time.sleep).
        monotonic: injectable clock a claim's landing is timed on (defaults to
            time.monotonic).

    Raises:
        ValueError: No database was named, or the lease is too short to renew: a
            renewal comes every third of it and needs a settle and a quorum wait left.
    """

    def __init__(
        self,
        ch: Any,
        database: str,
        *,
        worker_id: str = "worker-0",
        lease_seconds: int = 300,
        settle_seconds: float = SETTLE_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not database:
            raise ValueError("ChCoordinator requires an explicit data database")
        # A renewal held up by its quorum wait must still land before the marker lapses.
        margin = settle_seconds + QUORUM_TIMEOUT_MS / 1000
        if lease_seconds <= 3 * margin:
            raise ValueError(
                f"a {lease_seconds}s hunt lease never renews: renewals come every third of "
                f"it and each needs {margin:g}s left, so it must exceed {3 * margin:g}s"
            )
        self._margin = margin
        self._ch = ch
        self._db = database
        self._qdb = quote_identifier(database)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._settle = settle_seconds
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self._lease_settings: dict[str, Any] | None = None

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
        """The current lease: the hunt's latest row, read through ``FINAL``.

        ``FINAL`` keeps the row a merge keeps, so every reader names the same claim of a
        same-millisecond tie, before and after that merge. A replica missing an
        acknowledged lease write refuses the read rather than answer without it. A row
        whose owner's marker for that fire has ended reads as released, lease_until 0.
        """
        rows = self._ch.query(
            "SELECT l.owner, l.fire, if(m.owner = l.owner AND m.fire = l.fire, "  # noqa: S608 - quoted configured database, values bound
            "least(l.lease_until, m.lease_until), 0) "
            f"FROM (SELECT owner, fire, lease_until FROM {self._qdb}.hunt_lease FINAL "
            "WHERE hunt_id = {h:String}) AS l "
            f"LEFT JOIN (SELECT owner, fire, lease_until FROM {self._qdb}.hunt_lease FINAL "
            "WHERE startsWith(hunt_id, {prefix:String})) AS m ON m.owner = l.owner",
            parameters={"h": hunt_id, "prefix": f"{hunt_id}{_MARKER}"},
            settings=READ_SETTINGS,
        ).result_rows
        if not rows:
            return None
        return Lease(
            hunt_id=hunt_id,
            owner=rows[0][0],
            fire=int(rows[0][1]),
            lease_until=int(rows[0][2]),
        )

    def _lease_writes(self) -> dict[str, Any]:
        """Insert settings for every lease write, read from the server once.

        A re-claim or a release of the same fire repeats an earlier row, and it has to
        land. A Replicated table drops an INSERT VALUES identical to a recent one, and
        an older server (24.8) drops an identical INSERT ... SELECT too. Every write
        also waits for the lease quorum.
        """
        if self._lease_settings is None:
            self._lease_settings = {
                **keep_identical_rows(self._ch),
                **write_settings(self._ch, database=self._db, table="hunt_lease"),
            }
        return self._lease_settings

    def _marker(self, hunt_id: str) -> str:
        """The key of this worker's marker on *hunt_id*."""
        return f"{hunt_id}{_MARKER}{self._worker_id}"

    def _write_lease(self, rows: list[list[Any]]) -> None:
        """Write *rows* to hunt_lease in one insert."""
        self._ch.insert(
            "hunt_lease",
            rows,
            column_names=_LEASE_COLUMNS,
            database=self._db,
            settings=self._lease_writes(),
        )

    def _end_marker(self, hunt_id: str, fire: int) -> None:
        """Mark this worker's claim on *hunt_id* as holding nothing, leaving its row alone."""
        self._write_lease([[self._marker(hunt_id), self._worker_id, fire, 0]])

    def _end_marker_quietly(self, hunt_id: str, fire: int) -> None:
        """End this worker's marker, or log that peers wait out its lease when that write fails."""
        try:
            self._end_marker(hunt_id, fire)
        except Exception as exc:  # the claim already missed, so a failed end only delays peers
            logger.warning(
                "missed hunt claim not marked ended; peers wait out its lease",
                hunt_id=hunt_id,
                fire=fire,
                error=str(exc),
            )

    def _peer_holds(self, hunt_id: str, now: int) -> bool:
        """Whether another worker's marker on *hunt_id* is live at *now*."""
        rows = self._ch.query(
            f"SELECT count() FROM {self._qdb}.hunt_lease FINAL "  # noqa: S608 - quoted configured database, values bound
            "WHERE startsWith(hunt_id, {prefix:String}) AND owner != {o:String} "
            "AND lease_until > {now:Int64}",
            parameters={"prefix": f"{hunt_id}{_MARKER}", "o": self._worker_id, "now": now},
            settings=READ_SETTINGS,
        ).result_rows
        return bool(rows) and int(rows[0][0]) > 0

    def _own_marker(self, hunt_id: str) -> tuple[int, int] | None:
        """This worker's marker on *hunt_id* as ``(fire, lease_until)``, 0 once ended."""
        rows = self._ch.query(
            f"SELECT fire, lease_until FROM {self._qdb}.hunt_lease FINAL "  # noqa: S608 - quoted configured database, values bound
            "WHERE hunt_id = {m:String}",
            parameters={"m": self._marker(hunt_id)},
            settings=READ_SETTINGS,
        ).result_rows
        return (int(rows[0][0]), int(rows[0][1])) if rows else None

    def _latest_is_ours(self, hunt_id: str) -> bool:
        """Whether the hunt's latest row is this worker's."""
        lease = self.current_lease(hunt_id)
        return lease is not None and lease.owner == self._worker_id

    def try_claim(self, hunt_id: str, fire: int, now: int | None = None) -> bool:
        """Claim a hunt for this worker under the shared lease discipline. True if we own it.

        Returns False if another worker holds an active lease (an overrun - the caller
        records it and does NOT run), if we lost the settle race, or if our claim landed
        a settle or more after its free read. That last claim holds nothing once its
        marker has ended, so the next poll claims the hunt again.
        """
        now = self.now() if now is None else now
        # Read once per coordinator, before the clock starts, so it never makes a claim slow.
        self._lease_writes()
        current = self.current_lease(hunt_id)
        if current is not None and current.lease_until > now and current.owner != self._worker_id:
            return False  # someone else is running this hunt -> never double-run
        # Timed from the last free read, the one a rival's claim must have landed after.
        started = self._monotonic()
        if self._peer_holds(hunt_id, now):
            return False
        until = now + self._lease_seconds
        claim = [hunt_id, self._worker_id, fire, until]
        marker = [self._marker(hunt_id), self._worker_id, fire, until]
        try:
            self._write_lease([claim, marker])
        except Exception as exc:
            if is_quorum_refusal(exc):
                self._end_marker_quietly(hunt_id, fire)
            raise
        try:
            return settle_claim(
                started,
                stands=lambda: self._latest_is_ours(hunt_id),
                end=lambda: self._end_marker_quietly(hunt_id, fire),
                settle=self._settle,
                monotonic=self._monotonic,
                sleep=self._sleep,
            )
        except SlowClaimError as exc:
            logger.warning(
                f"hunt claim landed {exc.landed:.3f}s after its free read, too late to stand",
                hunt_id=hunt_id,
                fire=fire,
                landed_seconds=round(exc.landed, 3),
            )
            return False

    def renew(self, hunt_id: str, fire: int, now: int | None = None) -> bool:
        """Extend our claim on *fire* by a full lease from *now* while our marker has time left.

        The check runs on the server's clock when the statement starts, and needs more
        than a settle and a quorum wait left. A renewal delayed in transit, or held up by
        its quorum, cannot land after the marker lapsed and a peer claimed. The claim row
        is written again beside the marker, so it is the latest row once more after a
        missed claim landed over it.

        Returns:
            Whether our marker now carries this renewal. False once it had lapsed, had
            too little left, or had ended, when a peer may hold the hunt.
        """
        now = self.now() if now is None else now
        until = now + self._lease_seconds
        self._ch.command(
            f"INSERT INTO {self._qdb}.hunt_lease (hunt_id, owner, fire, lease_until) "  # noqa: S608 - quoted configured database, values bound
            "SELECT arrayJoin([{h:String}, {m:String}]), {o:String}, {f:Int64}, {u:Int64} "
            f"WHERE (SELECT count() FROM {self._qdb}.hunt_lease FINAL "
            "WHERE hunt_id = {m:String} AND fire = {f:Int64} "
            "AND lease_until > toUnixTimestamp(now64()) + {margin:Float64}) = 1",
            parameters={
                "h": hunt_id,
                "m": self._marker(hunt_id),
                "o": self._worker_id,
                "f": fire,
                "u": until,
                "margin": self._margin,
            },
            settings={**self._lease_writes(), **READ_SETTINGS},
        )
        return self._own_marker(hunt_id) == (fire, until)

    def release(self, hunt_id: str, fire: int) -> None:
        """Free the hunt if our row for *fire* is still the newest, and end our marker for it.

        The check and the insert are one statement, so an owner whose lease ran out and
        was claimed by a peer writes nothing over the peer's lease.
        """
        self._ch.command(
            f"INSERT INTO {self._qdb}.hunt_lease (hunt_id, owner, fire, lease_until) "  # noqa: S608 - quoted configured database, values bound
            "SELECT {h:String}, {o:String}, {f:Int64}, 0 "
            f"WHERE (SELECT count() FROM {self._qdb}.hunt_lease FINAL "
            "WHERE hunt_id = {h:String} AND owner = {o:String} AND fire = {f:Int64}) = 1",
            parameters={"h": hunt_id, "o": self._worker_id, "f": fire},
            settings={**self._lease_writes(), **READ_SETTINGS},
        )
        self._end_marker(hunt_id, fire)

    @contextmanager
    def held(self, hunt_id: str, fire: int) -> Generator[None]:
        """Keep our claim on *fire* live while the body runs, then release it.

        Renewals run on their own thread every third of the lease, so one statement
        outlasting the lease does not leave the hunt claimable while they land. Once
        they stop landing the body runs on unfenced, and a peer may claim the hunt.
        """
        stop = threading.Event()
        renewer = threading.Thread(
            target=self._keep_renewing,
            args=(hunt_id, fire, stop),
            name=f"hunt-lease-{hunt_id}",
            daemon=True,
        )
        renewer.start()
        try:
            yield
        finally:
            stop.set()
            renewer.join()
            self.release(hunt_id, fire)

    def _keep_renewing(self, hunt_id: str, fire: int, stop: threading.Event) -> None:
        """Renew until *stop* is set or our marker has lapsed, logging a failing streak once."""
        failing = False
        while not stop.wait(self._lease_seconds / 3):
            try:
                still_ours = self.renew(hunt_id, fire)
            except Exception as exc:  # the run goes on, and the next renewal may land
                if not failing:
                    logger.warning(
                        "hunt lease renewal failed", hunt_id=hunt_id, fire=fire, error=str(exc)
                    )
                failing = True
                continue
            if failing:
                logger.info("hunt lease renewal recovered", hunt_id=hunt_id, fire=fire)
            failing = False
            if not still_ours:
                logger.error(
                    "hunt lease lapsed mid-run; another runner may claim the hunt",
                    hunt_id=hunt_id,
                    fire=fire,
                    owner=self._worker_id,
                )
                return

    def active_count(self, now: int | None = None) -> int:
        """Hunts with a live claim marker right now - the input to the global cap.

        A dropped claim's row is not counted, since its ended marker holds nothing.
        """
        now = self.now() if now is None else now
        rows = self._ch.query(
            "SELECT uniqExact(substring(hunt_id, 1, position(hunt_id, {marker:String}) - 1)) "  # noqa: S608 - quoted configured database, values bound
            f"FROM {self._qdb}.hunt_lease FINAL "
            "WHERE position(hunt_id, {marker:String}) > 0 AND lease_until > {now:Int64}",
            parameters={"now": now, "marker": _MARKER},
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
            f"SELECT watermark FROM {self._qdb}.hunt_watermark "  # noqa: S608 - quoted configured database, values bound
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

    def get_safe_horizon_mark(self, hunt_id: str, mark: str) -> int | None:
        """A safe horizon mark for the hunt (``deferred_since`` or ``safe_from``), or None.

        Kept in hunt_watermark under ``<hunt_id>#<mark>``. A hunt id never holds ``#``,
        and every reader of that table looks up a hunt's own id, so a mark is never
        taken for a watermark. 0 is a cleared mark.
        """
        return self.get_watermark(f"{hunt_id}#{mark}") or None

    def set_safe_horizon_mark(self, hunt_id: str, mark: str, value: int) -> None:
        """Set a safe horizon mark for the hunt, or clear it with 0."""
        self.set_watermark(f"{hunt_id}#{mark}", value)

    # ---- state (UI overrun signal) ------------------------------------

    def get_state(self, hunt_id: str) -> HuntStateRow | None:
        """The hunt's persisted overrun / too-aggressive signal, if any."""
        rows = self._ch.query(
            f"SELECT overrun_count, too_aggressive FROM {self._qdb}.hunt_state "  # noqa: S608 - quoted configured database, values bound
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
            "SELECT r.hunt_id, r.fire FROM ("  # noqa: S608 - quoted configured database, values bound
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

        Read-then-insert (CH has no atomic increment). The count is a soft UI
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
