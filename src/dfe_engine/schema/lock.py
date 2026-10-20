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
earlier one goes back to waiting.

The boot phase holds it, and so does every source deploy (:func:`ddl_lease`).
"""

import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from scalo.logger import logger

from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.schema.plan import LOCK_ID, render_one

LOCK_NAME = "schema_bootstrap"

_POLL_SECONDS = 2.0

# Longer than a racing claimer takes between finding the lease free and writing.
_SETTLE_SECONDS = 0.5


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


def holder_name() -> str:
    """Who this process is in the lease: the pod hostname, which is what an operator reads."""
    return socket.gethostname()


class SchemaLock:
    """Take, hold and release the bootstrap lease.

    Construct with the table's real name, which the caller reads off the rendered
    manifest object -- this module names no table of its own.
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
        self._holder = holder or holder_name()
        self._lease_seconds = max(lease_seconds, 1)
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
        """The latest claim on the lock name, or None when nothing has ever taken it."""
        target = f"{quote_identifier(self._database)}.{quote_identifier(self._table)}"
        sql = (
            "SELECT argMax(holder, acquired_at), argMax(expires_at, acquired_at), "  # noqa: S608 - quoted database and manifest table; values bound
            f"argMax(released, acquired_at), max(acquired_at) FROM {target} "
            "WHERE lock_name = {lock:String}"
        )
        try:
            rows = list(self._client.query(sql, parameters={"lock": LOCK_NAME}).result_rows)
        except Exception as exc:
            raise SchemaLockError(f"could not read the schema lock: {exc}") from exc
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
        somebody else's to converge, not this caller's to race on.
        """
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while True:
            state = self.read()
            now = datetime.now(UTC)
            if state is None or not state.held_at(now):
                claimed_at = self._claim(now)
                time.sleep(_SETTLE_SECONDS)
                if self._claim_stands(claimed_at):
                    self.acquired = True
                    return True
                if time.monotonic() >= deadline:
                    return False
                continue
            if time.monotonic() >= deadline:
                logger.info(
                    "schema lock held elsewhere; reporting without applying",
                    holder=state.holder,
                    expires_at=state.expires_at.isoformat(),
                )
                return False
            time.sleep(_POLL_SECONDS)

    def release(self) -> None:
        """Soft-release the lease. A completed pass needs no delete."""
        if not self.acquired:
            return
        self._write(datetime.now(UTC), expires=datetime.now(UTC), released=1)
        self.acquired = False

    def _claim(self, now: datetime) -> datetime:
        """Write a claim and return its acquired_at as the table stores it (milliseconds)."""
        acquired = now.replace(microsecond=now.microsecond // 1000 * 1000)
        self._write(acquired, expires=now + timedelta(seconds=self._lease_seconds), released=0)
        return acquired

    def _claim_stands(self, claimed_at: datetime) -> bool:
        """Whether no other claimer wrote at or after this claim, so the latest is still ours.

        A read that does not show this claim yet is no sign of a later one.
        """
        state = self.read()
        if state is None or state.acquired_at < claimed_at:
            return True
        return state.holder == self._holder and state.acquired_at == claimed_at

    def _write(self, acquired: datetime, *, expires: datetime, released: int) -> None:
        try:
            self._client.insert(
                self._table,
                [[LOCK_NAME, self._holder, acquired, expires.replace(microsecond=0), released]],
                column_names=["lock_name", "holder", "acquired_at", "expires_at", "released"],
                database=self._database,
            )
        except Exception as exc:
            raise SchemaLockError(f"could not write the schema lock: {exc}") from exc

    def __enter__(self) -> SchemaLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


@contextmanager
def ddl_lease(client: Any, *, data_database: str, wait_seconds: float) -> Iterator[None]:
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
        # A failed release only delays the next applier until the claim lapses.
        try:
            lock.release()
        except SchemaLockError as exc:
            logger.warning("schema lease not released; it lapses at its expiry", error=str(exc))
