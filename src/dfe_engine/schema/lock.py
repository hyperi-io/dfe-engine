#  Project:      dfe-engine
#  File:         schema/lock.py
#  Purpose:      The ClickHouse lease that keeps one schema applier at a time
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One applier at a time, through a lease in ``schema_lock``.

``CREATE TABLE IF NOT EXISTS`` races benignly. Concurrent ``ALTER TABLE ADD
COLUMN`` against one table does not, and that is what this exists to stop: a
second engine replica, a hunt runner and an operator running ``dfe schema apply``
by hand all reach the same tables.

The lease is a holder plus an expiry, the shape the hunt runner already trusts. A
caller that cannot take it waits for the holder to finish and then takes it. One
still waiting when its wait runs out applies nothing. An expired lease is taken
over rather than waited on, so a crashed holder does not block the next boot.

The table keeps the latest write per lock name, so two callers that both found the
lease free both write a claim. Each re-reads after a settle delay and keeps the
lease only if the latest claim is still its own, so the later claim wins and the
earlier one goes back to waiting. Reads use ``FINAL``, so a same-millisecond tie
resolves to the row a merge keeps, before and after that merge. Each lock instance
writes its own holder name, so two claimers on one pod still tell their claims apart.

Every claim reaches a majority of the table's replicas before ClickHouse acknowledges
it, and every read answers only from a replica holding all of them, so a re-read that
lands on a lagging replica still sees the claim that beat it. A claim that cannot
reach its majority, with a replica down, is a missed attempt retried within the wait.
So is one acknowledged a settle or more after its free read: a rival's re-read may
have missed it, so it is not kept.

A missed claim is never released, because it may be the latest row while a rival
stood under it, and a release would read as a free lease. Instead each claim also
writes a marker of its own under ``<lock name>/claim/<holder>/<acquired_at>``, which
no merge collapses into another. The marker stays live while its claim holds and is
ended when the claim loses or misses. A live claim row whose marker is ended blocks
nobody, so racers whose first claims all miss claim again rather than wait out a lease.
A live marker always blocks, so a holder stays visible after a merge keeps only a
later row.

The boot phase holds it, and so does every source deploy (:func:`ddl_lease`).
"""

import secrets
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from scalo.logger import logger

from dfe_engine.clickhouse.errors import parse_code
from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.schema.plan import LOCK_ID, render_one

LOCK_NAME = "schema_bootstrap"

_POLL_SECONDS = 2.0

# A claim stands only if acknowledged within this long of its free read, so every rival sees it.
# A quorum write to a second replica takes up to about a second, so this clears it twice over.
_SETTLE_SECONDS = 2.0

# A lease read answers only from a replica holding every claim a quorum acknowledged.
_READ_SETTINGS = {"select_sequential_consistency": 1}

# A claim acknowledged after its lease lapsed could be taken over, so this ends well inside a lease.
_QUORUM_TIMEOUT_MS = 10_000

# A missed quorum, which acquire retries within its wait: TOO_FEW_LIVE_REPLICAS,
# UNSATISFIED_QUORUM_FOR_PREVIOUS_WRITE, REPLICA_IS_NOT_IN_QUORUM, UNKNOWN_STATUS_OF_INSERT.
_QUORUM_REFUSALS = frozenset({285, 286, 289, 319})


class SchemaLockError(Exception):
    """The lease could not be read or written."""


class SchemaLockBusyError(SchemaLockError):
    """Another applier held the lease for the whole wait.

    Attributes:
        holder: Who held it when the wait ran out, empty when that could not be read.
    """

    def __init__(self, holder: str) -> None:
        super().__init__(f"the schema lease is held by {holder or 'another applier'}")
        self.holder = holder


class SchemaLockQuorumError(SchemaLockError):
    """A lease read or write missed its quorum of replicas, so that attempt did not complete."""


class _SlowClaim(Exception):
    """A claim acknowledged a settle or more after its free read, left to lapse unkept."""


def is_quorum_refusal(exc: BaseException) -> bool:
    """Whether *exc* is ClickHouse refusing a missed quorum, which a later attempt can clear."""
    return parse_code(exc) in _QUORUM_REFUSALS


def _lock_error(action: str, exc: Exception) -> SchemaLockError:
    """*exc* as a lease error, with a missed quorum kept apart so a claimer retries it."""
    kind = SchemaLockQuorumError if is_quorum_refusal(exc) else SchemaLockError
    return kind(f"{action}: {exc}")


def _majority(replicas: int) -> int:
    """A majority of *replicas*: 1 on one node, which ClickHouse reads as no quorum wait."""
    return max(replicas, 1) // 2 + 1


def _write_settings_for(engine: str, replicas: int) -> dict[str, int]:
    """What a lease write carries on a table of *engine* with *replicas* replicas.

    Every SharedMergeTree insert is already a quorum insert and ClickHouse documents
    the quorum settings as not applicable to it, so they are left off there.
    ``insert_quorum = 'auto'`` never resolves on a single replica and refuses every
    later write behind it, so elsewhere the majority is counted. ClickHouse refuses a
    sequential quorum on an async insert, so every write is synchronous.
    """
    if engine.startswith("Shared"):
        return {"async_insert": 0}
    return {
        "insert_quorum": _majority(replicas),
        "insert_quorum_parallel": 0,
        "insert_quorum_timeout": _QUORUM_TIMEOUT_MS,
        "async_insert": 0,
    }


def lease_seconds(wait_seconds: float) -> int:
    """A claim's length: twice a peer's wait, so a live holder is waited on, never taken over."""
    return int(max(wait_seconds, 60.0) * 2)


@dataclass(frozen=True)
class LockState:
    """What the lease table holds for one lock name."""

    holder: str
    expires_at: datetime
    released: bool
    acquired_at: datetime

    def held_at(self, now: datetime) -> bool:
        """Whether this row is a live claim rather than a finished or lapsed one."""
        return not self.released and self.expires_at > now


def _utc(value: datetime) -> datetime:
    """*value* as an aware UTC time, since the driver hands back naive UTC times."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _millis(value: datetime) -> datetime:
    """*value* to the millisecond the table's ``DateTime64(3)`` stores."""
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def holder_name() -> str:
    """The pod hostname, which every holder name this process writes starts with."""
    return socket.gethostname()


class SchemaLock:
    """Take, hold and release the bootstrap lease.

    Construct with the table's real name, which the caller reads off the rendered
    manifest object -- this module names no table of its own. With no *holder*,
    the instance writes the hostname plus a suffix of its own.
    """

    def __init__(
        self,
        client: Any,
        *,
        database: str,
        table: str,
        holder: str | None = None,
        lease_seconds: int = 300,
    ) -> None:
        self._client = client
        self._database = database
        self._table = table
        # Concurrent deploys on one pod each build a lock, and a tie needs their names to differ.
        self._holder = holder or f"{holder_name()}/{secrets.token_hex(4)}"
        self._lease_seconds = max(lease_seconds, 1)
        self._claimed_at: datetime | None = None
        self.acquired = False

    @property
    def holder(self) -> str:
        """The holder name this lock writes."""
        return self._holder

    def table_exists(self) -> bool:
        """Whether the lease table is on the server, which it is not before a first boot."""
        try:
            rows = self._client.query(
                "SELECT 1 FROM system.tables WHERE database = {db:String} "
                "AND name = {tbl:String} LIMIT 1",
                parameters={"db": self._database, "tbl": self._table},
            ).result_rows
        except Exception as exc:
            raise SchemaLockError(f"could not read the schema lock table: {exc}") from exc
        return bool(rows)

    def read(self) -> LockState | None:
        """The latest claim on the lock name, or None when nothing has ever taken it.

        ``FINAL`` keeps the row a merge would keep, so readers name the same claim
        of a tie whether or not it has merged yet. A replica missing an acknowledged
        claim refuses the read rather than answer without it.
        """
        target = f"{quote_identifier(self._database)}.{quote_identifier(self._table)}"
        sql = (
            f"SELECT holder, expires_at, released, acquired_at FROM {target} FINAL "  # noqa: S608 - quoted database and manifest table; values bound
            "WHERE lock_name = {lock:String}"
        )
        try:
            result = self._client.query(
                sql, parameters={"lock": LOCK_NAME}, settings=_READ_SETTINGS
            )
            rows = list(result.result_rows)
        except Exception as exc:
            raise _lock_error("could not read the schema lock", exc) from exc
        if not rows or rows[0][0] in (None, ""):
            return None
        expires, acquired = rows[0][1], rows[0][3]
        if not isinstance(expires, datetime) or not isinstance(acquired, datetime):
            return None
        return LockState(
            holder=str(rows[0][0]),
            expires_at=_utc(expires),
            released=bool(rows[0][2]),
            acquired_at=_utc(acquired),
        )

    def acquire(self, *, wait_seconds: float = 0.0) -> bool:
        """Take the lease, waiting up to *wait_seconds* for a live holder to finish.

        Returns whether this process now holds it. False means another process is
        mid-apply and this one must report rather than apply -- the schema is
        somebody else's to converge, not this caller's to race on. An attempt that
        misses its quorum, with a replica down or another claim still reaching its
        own, or whose claim lands too slowly to stand, is retried within the wait and
        gives False once the wait runs out.
        """
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        missed: set[type[Exception]] = set()
        while True:
            try:
                started = time.monotonic()
                state = self.read()
                now = datetime.now(UTC)
                if state is None or not state.held_at(now) or self._only_ended_claims(now):
                    if self._try_claim(now, started=started):
                        return True
                    if time.monotonic() >= deadline:
                        return False
                    continue
            except (SchemaLockQuorumError, _SlowClaim) as exc:
                # Once per wait and cause: a down replica stays down for every attempt in it.
                if type(exc) not in missed:
                    logger.warning(
                        "schema lease attempt missed; retrying within the wait", reason=str(exc)
                    )
                missed.add(type(exc))
                state = None
            if time.monotonic() >= deadline:
                if state is not None:
                    logger.info(
                        "schema lock held elsewhere; reporting without applying",
                        holder=state.holder,
                        expires_at=state.expires_at.isoformat(),
                    )
                return False
            time.sleep(_POLL_SECONDS)

    def release(self) -> None:
        """Soft-release the lease. A completed pass needs no delete."""
        if not self.acquired or self._claimed_at is None:
            return
        # The claim's own version, so a takeover written after this claim lapsed still outranks it.
        claimed, ends = self._claimed_at, datetime.now(UTC)
        self._write_rows(
            [
                self._row(LOCK_NAME, claimed, ends, 1),
                self._row(self._marker(claimed), claimed, ends, 1),
            ]
        )
        self._claimed_at = None
        self.acquired = False

    def release_or_lapse(self) -> bool:
        """Release the lease, or log that it lapses at its expiry when the release fails.

        Returns:
            Whether the release was written. A failed one only delays the next applier.
        """
        try:
            self.release()
        except SchemaLockError as exc:
            logger.warning("schema lease not released; it lapses at its expiry", error=str(exc))
            return False
        return True

    def _try_claim(self, now: datetime, *, started: float) -> bool:
        """Claim, let racing claimers land theirs, and keep the claim if it is still the latest.

        A claim that misses or loses has its marker ended, so it holds nobody back.

        Raises:
            _SlowClaim: The claim was acknowledged a settle or more after *started*,
                the free read's start, so it was left to lapse at its expiry.
            SchemaLockQuorumError: The claim or its marker missed its quorum.
        """
        claimed_at = _millis(now)
        expires = now + timedelta(seconds=self._lease_seconds)
        try:
            self._claim(now)
        except SchemaLockQuorumError:
            self._end_marker_quietly(claimed_at, expires)
            raise
        landed = time.monotonic() - started
        if landed >= _SETTLE_SECONDS:
            self._end_marker_quietly(claimed_at, expires)
            raise _SlowClaim(f"the claim landed {landed:.2f}s after its free read")
        time.sleep(_SETTLE_SECONDS)
        if not self._claim_stands(claimed_at):
            self._end_marker_quietly(claimed_at, expires)
            return False
        self._claimed_at = claimed_at
        self.acquired = True
        return True

    def _marker(self, claimed_at: datetime) -> str:
        """The lock name of this lock's marker for the claim made at *claimed_at*."""
        return f"{LOCK_NAME}/claim/{self._holder}/{claimed_at.isoformat(timespec='milliseconds')}"

    def _end_marker(self, claimed_at: datetime, expires: datetime | None = None) -> None:
        """Mark the claim made at *claimed_at* as holding nothing."""
        ends = expires if expires is not None else datetime.now(UTC)
        self._write(claimed_at, expires=ends, released=1, lock_name=self._marker(claimed_at))

    def _end_marker_quietly(self, claimed_at: datetime, expires: datetime) -> None:
        """End a claim's marker, or log that rivals wait out its lease when that write fails."""
        try:
            self._end_marker(claimed_at, expires)
        except SchemaLockError as exc:
            logger.warning(
                "missed schema lease claim not marked ended; rivals wait out its lease",
                error=str(exc),
            )

    def _only_ended_claims(self, now: datetime) -> bool:
        """Whether every live claim on the lease is one its claimer marked ended, with no live marker.

        Reads rows a merge has not yet collapsed, so a claim kept only under a later
        row is still caught by its marker, which sits under a lock name of its own.
        """
        target = f"{quote_identifier(self._database)}.{quote_identifier(self._table)}"
        claims = (
            f"SELECT holder, acquired_at FROM {target} "  # noqa: S608 - quoted database and manifest table; values bound
            "WHERE lock_name = {lock:String} GROUP BY holder, acquired_at "
            "HAVING max(released) = 0 AND max(expires_at) > {now:DateTime}"
        )
        markers = (
            f"SELECT holder, acquired_at, max(released), max(expires_at) > {{now:DateTime}} FROM {target} "  # noqa: S608 - quoted database and manifest table; values bound
            "WHERE startsWith(lock_name, {prefix:String}) GROUP BY holder, acquired_at"
        )
        parameters = {
            "lock": LOCK_NAME,
            "prefix": f"{LOCK_NAME}/claim/",
            "now": now.replace(tzinfo=None),
        }
        try:
            live = self._client.query(claims, parameters=parameters, settings=_READ_SETTINGS)
            marked = self._client.query(markers, parameters=parameters, settings=_READ_SETTINGS)
        except Exception as exc:
            raise _lock_error("could not read the schema lock", exc) from exc
        ended: set[tuple[str, datetime]] = set()
        for holder, acquired, released, unexpired in marked.result_rows:
            if released:
                ended.add((str(holder), _utc(acquired)))
            elif unexpired:
                return False
        return all((str(holder), _utc(acquired)) in ended for holder, acquired in live.result_rows)

    def _claim(self, now: datetime) -> datetime:
        """Write a claim and its marker, and return its acquired_at as the table stores it.

        One insert carries both rows, so they land in one part under one quorum.
        """
        acquired = _millis(now)
        expires = now + timedelta(seconds=self._lease_seconds)
        self._write_rows(
            [
                self._row(LOCK_NAME, acquired, expires, 0),
                self._row(self._marker(acquired), acquired, expires, 0),
            ]
        )
        return acquired

    def _claim_stands(self, claimed_at: datetime) -> bool:
        """Whether the latest visible claim is this one, or one written before it.

        An earlier claim means this one is not visible yet, which is no sign of a later one.
        """
        state = self.read()
        if state is None or state.acquired_at < claimed_at:
            return True
        return state.acquired_at == claimed_at and state.holder == self._holder

    def _write_settings(self) -> dict[str, int]:
        """The settings :func:`_write_settings_for` picks for the lease table as it stands."""
        parameters = {"db": self._database, "tbl": self._table}
        try:
            engines = self._client.query(
                "SELECT engine FROM system.tables WHERE database = {db:String} "
                "AND name = {tbl:String}",
                parameters=parameters,
            ).result_rows
            engine = str(engines[0][0]) if engines else ""
            if engine.startswith("Shared"):
                return _write_settings_for(engine, 0)
            replicas = self._client.query(
                "SELECT total_replicas FROM system.replicas "
                "WHERE database = {db:String} AND table = {tbl:String}",
                parameters=parameters,
            ).result_rows
        except Exception as exc:
            raise _lock_error("could not read the schema lock's engine and replicas", exc) from exc
        return _write_settings_for(engine, int(replicas[0][0]) if replicas else 1)

    def _row(
        self, lock_name: str, acquired: datetime, expires: datetime, released: int
    ) -> list[Any]:
        return [lock_name, self._holder, acquired, expires.replace(microsecond=0), released]

    def _write(
        self, acquired: datetime, *, expires: datetime, released: int, lock_name: str = LOCK_NAME
    ) -> None:
        self._write_rows([self._row(lock_name, acquired, expires, released)])

    def _write_rows(self, rows: list[list[Any]]) -> None:
        settings = self._write_settings()
        try:
            self._client.insert(
                self._table,
                rows,
                column_names=["lock_name", "holder", "acquired_at", "expires_at", "released"],
                database=self._database,
                settings=settings,
            )
        except Exception as exc:
            raise _lock_error("could not write the schema lock", exc) from exc

    def __enter__(self) -> SchemaLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@contextmanager
def ddl_lease(client: Any, *, data_database: str, wait_seconds: float) -> Generator[None]:
    """Hold the schema lease around DDL applied outside the boot phase.

    A server without the lease table has never run the phase, so there is no lease
    to take and the block runs unheld, the same answer the phase gives a first boot.

    Args:
        client: A live ClickHouse client.
        data_database: The deployment's data database, which carries the lease table.
        wait_seconds: How long to wait for a live holder to finish.

    Raises:
        SchemaLockBusyError: Another applier held the lease for the whole wait.
        SchemaLockError: The lease could not be read or taken.
    """
    lock_object = render_one(LOCK_ID, data_database=data_database)
    lock = SchemaLock(
        client,
        database=lock_object.database or data_database,
        table=lock_object.name,
        lease_seconds=lease_seconds(wait_seconds),
    )
    if not lock.table_exists():
        yield
        return
    if not lock.acquire(wait_seconds=wait_seconds):
        held = lock.read()
        raise SchemaLockBusyError(held.holder if held is not None else "")
    try:
        yield
    finally:
        lock.release_or_lapse()
