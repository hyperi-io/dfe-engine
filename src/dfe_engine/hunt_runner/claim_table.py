#  Project:      dfe-engine
#  File:         hunt_runner/claim_table.py
#  Purpose:      PG claim-table for pull-based, no-double-run hunt distribution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""PostgreSQL claim table - the pull-based distribution substrate.

Workers PULL due runs via SELECT ... FOR UPDATE SKIP LOCKED, so runs are NEVER
assigned to pods (no static shard, no brittle rebalance) - add a worker and it just
pulls; kill one and its lease expires and is reclaimed. The scheduler enqueues due
runs; the global cap is applied as the claim LIMIT (cap - running).

Takes a psycopg connection; SQL is module-level so it is unit-testable without a DB,
and the concurrency behaviour is covered by an integration test against real PG.
"""

from __future__ import annotations

from typing import Any

CREATE_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS hunt_run (
        id          BIGSERIAL PRIMARY KEY,
        hunt_id     TEXT   NOT NULL,
        due_at      BIGINT NOT NULL,
        state       TEXT   NOT NULL DEFAULT 'pending',
        worker_id   TEXT,
        lease_until BIGINT
    )
    """,
    "CREATE INDEX IF NOT EXISTS hunt_run_pending ON hunt_run (state, due_at)",
    """
    CREATE TABLE IF NOT EXISTS hunt_state (
        hunt_id           TEXT PRIMARY KEY,
        status            TEXT   NOT NULL DEFAULT 'idle',
        last_completed_at BIGINT,
        overrun_count     INT    NOT NULL DEFAULT 0,
        too_aggressive    BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
]

# Atomic claim: take up to N due 'pending' runs, mark them 'running' with a lease.
# FOR UPDATE SKIP LOCKED gives exactly-one-claim across concurrent workers.
CLAIM_SQL = """
UPDATE hunt_run SET state = 'running', worker_id = %(worker)s, lease_until = %(lease)s
WHERE id IN (
    SELECT id FROM hunt_run
    WHERE state = 'pending' AND due_at <= %(now)s
    ORDER BY due_at
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
RETURNING id, hunt_id, due_at
"""

COMPLETE_SQL = "UPDATE hunt_run SET state = 'done' WHERE id = %(id)s"

# Reclaim runs whose worker died (lease expired) back to 'pending'.
RECLAIM_SQL = """
UPDATE hunt_run SET state = 'pending', worker_id = NULL, lease_until = NULL
WHERE state = 'running' AND lease_until IS NOT NULL AND lease_until < %(now)s
RETURNING id
"""

ENQUEUE_SQL = "INSERT INTO hunt_run (hunt_id, due_at) VALUES (%(hunt_id)s, %(due_at)s) RETURNING id"

RUNNING_COUNT_SQL = "SELECT count(*) FROM hunt_run WHERE state = 'running'"


class ClaimTable:
    """Thin psycopg wrapper over the claim-table SQL (caller owns the connection)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def init_schema(self) -> None:
        with self._conn.cursor() as cur:
            for ddl in CREATE_DDL:
                cur.execute(ddl)
        self._conn.commit()

    def enqueue(self, hunt_id: str, due_at: int) -> int:
        with self._conn.cursor() as cur:
            cur.execute(ENQUEUE_SQL, {"hunt_id": hunt_id, "due_at": due_at})
            run_id = cur.fetchone()[0]
        self._conn.commit()
        return int(run_id)

    def claim(self, worker_id: str, now: int, limit: int, lease_seconds: int = 300) -> list[dict]:
        """Claim up to `limit` due runs for this worker (exactly-once across pods)."""
        with self._conn.cursor() as cur:
            cur.execute(
                CLAIM_SQL,
                {"worker": worker_id, "lease": now + lease_seconds, "now": now, "limit": limit},
            )
            rows = cur.fetchall()
        self._conn.commit()
        return [{"id": r[0], "hunt_id": r[1], "due_at": r[2]} for r in rows]

    def complete(self, run_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(COMPLETE_SQL, {"id": run_id})
        self._conn.commit()

    def reclaim_expired(self, now: int) -> int:
        with self._conn.cursor() as cur:
            cur.execute(RECLAIM_SQL, {"now": now})
            n = len(cur.fetchall())
        self._conn.commit()
        return n

    def running_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(RUNNING_COUNT_SQL)
            return int(cur.fetchone()[0])
