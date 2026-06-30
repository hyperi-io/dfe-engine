#  Project:      dfe-engine
#  File:         tests/integration/test_claim_table_pg.py
#  Purpose:      Live PostgreSQL test of the SKIP-LOCKED claim table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Integration test against a REAL PostgreSQL (no mocks).

Connection from the pg_dsn fixture (DFE_TEST_PG_DSN -> docker fallback -> skip).
Proves the exactly-once SKIP-LOCKED claim semantics + lease reclaim that the
pull-based distribution relies on. Cleans up its tables.
"""

from __future__ import annotations

import psycopg
import pytest

from dfe_engine.hunt_runner import ClaimTable


@pytest.fixture
def table(pg_conn):
    """Fresh claim-table schema on the live PG (drops first; drops after)."""
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS hunt_run")
        cur.execute("DROP TABLE IF EXISTS hunt_state")
    pg_conn.commit()
    t = ClaimTable(pg_conn)
    t.init_schema()
    try:
        yield t
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS hunt_run")
            cur.execute("DROP TABLE IF EXISTS hunt_state")
        pg_conn.commit()


def test_concurrent_workers_get_disjoint_claims(table, pg_dsn):
    for i in range(6):
        table.enqueue(f"hunt-{i}", due_at=100)

    # a second independent connection claims concurrently -> disjoint rows
    c2 = psycopg.connect(pg_dsn, autocommit=False)
    try:
        t2 = ClaimTable(c2)
        a = table.claim("w1", now=200, limit=3)
        b = t2.claim("w2", now=200, limit=3)
    finally:
        c2.close()

    ids_a = {r["id"] for r in a}
    ids_b = {r["id"] for r in b}
    assert ids_a
    assert ids_b
    assert ids_a.isdisjoint(ids_b)  # exactly-once: no overlap
    assert len(ids_a | ids_b) == 6


def test_not_due_runs_are_not_claimed(table):
    table.enqueue("future", due_at=10_000)
    assert table.claim("w1", now=100, limit=10) == []


def test_reclaim_expired_lease(table):
    table.enqueue("h", due_at=100)
    claimed = table.claim("w1", now=200, limit=1, lease_seconds=10)  # lease_until=210
    assert len(claimed) == 1
    assert table.claim("w2", now=205, limit=1) == []  # still running
    assert table.reclaim_expired(now=999) == 1  # lease expired -> reclaimed
    again = table.claim("w2", now=1000, limit=1)
    assert len(again) == 1
