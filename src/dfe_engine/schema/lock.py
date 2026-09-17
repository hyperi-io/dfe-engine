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
caller that cannot take it waits for the holder to finish, then reports without
applying. An expired lease is taken over rather than waited on, so a crashed
holder does not block the next boot.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from scalo.logger import logger

LOCK_NAME = "schema_bootstrap"

_POLL_SECONDS = 2.0


class SchemaLockError(Exception):
    """The lease could not be read or written."""


@dataclass(frozen=True)
class LockState:
    """What the lease table holds for one lock name."""

    holder: str
    expires_at: datetime
    released: bool

    def held_at(self, now: datetime) -> bool:
        """Whether this row is a live claim rather than a finished or lapsed one."""
        return not self.released and self.expires_at > now


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

    def read(self) -> LockState | None:
        """The latest claim on the lock name, or None when nothing has ever taken it."""
        sql = (
            "SELECT argMax(holder, acquired_at), argMax(expires_at, acquired_at), "
            f"argMax(released, acquired_at) FROM {self._database}.{self._table} "
            "WHERE lock_name = {lock:String}"
        )
        try:
            rows = list(self._client.query(sql, parameters={"lock": LOCK_NAME}).result_rows)
        except Exception as exc:
            raise SchemaLockError(f"could not read the schema lock: {exc}") from exc
        if not rows or rows[0][0] in (None, ""):
            return None
        expires = rows[0][1]
        if not isinstance(expires, datetime):
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return LockState(holder=str(rows[0][0]), expires_at=expires, released=bool(rows[0][2]))

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
                self._claim(now)
                self.acquired = True
                return True
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

    def _claim(self, now: datetime) -> None:
        self._write(now, expires=now + timedelta(seconds=self._lease_seconds), released=0)

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
