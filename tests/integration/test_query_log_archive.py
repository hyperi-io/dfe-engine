#  Project:      dfe-engine
#  File:         tests/integration/test_query_log_archive.py
#  Purpose:      query_log_archive MV round-trip on real CH (CANON 3-target matrix)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Live proof the query_log_archive MV captures a tagged query + the leaderboard.

Runs against the CANON matrix (local docker / cluster / cloud) via ``ch_conn``. A
tagged query goes through the canonical wrapper (so its ``log_comment`` carries the
attribution), ``SYSTEM FLUSH LOGS`` forces ``system.query_log`` to flush - which
fires the MV - and the archive + cost leaderboard are asserted to contain the row.
The engine form is resolved by sensing (MergeTree single / ReplicatedMergeTree
cluster / Shared cloud), so this is the net for the single -> cluster -> cloud
breakages the plan calls out.
"""

from __future__ import annotations

import time

import pytest

from dfe_engine.clickhouse.attribution import tags_context
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.query_log_archive import cost_leaderboard, ensure

pytestmark = pytest.mark.integration


def _wrapper(ch_conn: dict, database: str):
    """A canonical wrapper bound to the current matrix target + test database."""
    cfg = {
        "ch_host": ch_conn["host"],
        "ch_port": ch_conn["port"],
        "ch_username": ch_conn.get("username", "default"),
        "ch_password": ch_conn.get("password", ""),
        "ch_secure": ch_conn.get("secure", False),
        "ch_database": database,
    }
    return ClickHouseManager.get_instance(cfg).get_clickhouse_client()


def _poll_archive(wrapper, database: str, feature: str, *, attempts: int = 20):
    """Poll the archive for the tagged row after a flush (bounded backstop).

    FLUSH LOGS fires the MV synchronously on the connected node; a replicated
    target may take a beat to make the row visible, so poll instead of racing a
    single guess - returns as soon as the row lands.
    """
    sql = (
        f"SELECT dfe_id, read_rows, tenant_id FROM {database}.query_log_archive "
        "WHERE feature = {f:String}"
    )
    for _ in range(attempts):
        _, rows = wrapper.query_rows(sql, parameters={"f": feature})
        if rows:
            return rows
        time.sleep(0.5)
    return []


def test_query_log_archive_round_trip(ch_conn, clickhouse_test_database):
    db = clickhouse_test_database
    ClickHouseManager.reset_instance()
    wrapper = _wrapper(ch_conn, db)

    # 1) Create the archive table + MV, sensing the engine for this topology.
    ensure(wrapper, database=db)

    # 2) A uniquely-tagged query through the wrapper - its log_comment carries the
    #    attribution, and reading numbers() gives a non-zero read_rows to assert on.
    feature = f"itest_{ch_conn['id']}"
    hunt_id = "hunt-cost-xyz"
    with tags_context(feature=feature, id=hunt_id, tenant_id="acme", kind="read"):
        wrapper.query("SELECT count() FROM numbers(5000)")

    # 3) Force the query_log flush -> the MV fires and populates the archive.
    wrapper.command("SYSTEM FLUSH LOGS")

    rows = _poll_archive(wrapper, db, feature)
    assert rows, "tagged query did not reach the archive after FLUSH LOGS"
    dfe_id, read_rows, tenant_id = rows[0]
    assert dfe_id == hunt_id
    assert tenant_id == "acme"
    assert read_rows >= 5000  # the numbers(5000) scan cost was captured

    # 4) The cost leaderboard (the parked feature this unblocks) surfaces it.
    board = cost_leaderboard(wrapper, feature=feature, days=1, limit=10, database=db)
    assert any(entry["id"] == hunt_id for entry in board)
    top = next(e for e in board if e["id"] == hunt_id)
    assert top["read_rows"] >= 5000
    assert top["queries"] >= 1

    ClickHouseManager.reset_instance()
