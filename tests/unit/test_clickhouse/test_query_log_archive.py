#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_query_log_archive.py
#  Purpose:      query_log_archive DDL rendering - resolver-aware, per topology
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""query_log_archive: the DDL renders the right engine per topology + a valid MV."""

from __future__ import annotations

from dfe_engine.clickhouse.query_log_archive import render_ddl
from dfe_engine.schema.engine_resolver import ResolvedEngine


def test_single_node_renders_plain_mergetree():
    db_stmt, tbl_stmt, mv_stmt = render_ddl(None)
    assert db_stmt == "CREATE DATABASE IF NOT EXISTS dfe_audit"
    assert "ENGINE = MergeTree()" in tbl_stmt
    assert " ON CLUSTER " not in tbl_stmt
    # The MV lifts system.query_log into the archive target.
    assert "CREATE MATERIALIZED VIEW IF NOT EXISTS dfe_audit.query_log_archive_mv" in mv_stmt
    assert "TO dfe_audit.query_log_archive" in mv_stmt
    assert "FROM system.query_log" in mv_stmt


def test_replicated_on_cluster_engine_and_clause():
    engine = ResolvedEngine(
        clause="ReplicatedMergeTree",
        on_cluster=" ON CLUSTER default",
        topology="replicated",
        origin="sensed",
    )
    db_stmt, tbl_stmt, mv_stmt = render_ddl(engine)
    assert db_stmt == "CREATE DATABASE IF NOT EXISTS dfe_audit ON CLUSTER default"
    assert "ENGINE = ReplicatedMergeTree" in tbl_stmt
    assert "dfe_audit.query_log_archive ON CLUSTER default" in tbl_stmt
    # The MV must also carry ON CLUSTER so it lands on every replica.
    assert "query_log_archive_mv ON CLUSTER default" in mv_stmt


def test_log_comment_is_exploded_into_typed_columns():
    _, _, mv_stmt = render_ddl(None)
    for key in ("service", "tenant_id", "feature", "kind"):
        assert f"JSONExtractString(log_comment, '{key}')" in mv_stmt
    assert "JSONExtractString(log_comment, 'id') AS dfe_id" in mv_stmt


def test_mv_only_archives_finished_tagged_valid_rows():
    _, _, mv_stmt = render_ddl(None)
    assert "type = 'QueryFinish'" in mv_stmt
    assert "log_comment != ''" in mv_stmt
    assert "isValidJSON(log_comment)" in mv_stmt


def test_ttl_days_is_configurable():
    _, tbl_stmt, _ = render_ddl(None, ttl_days=7)
    assert "TTL event_time + INTERVAL 7 DAY" in tbl_stmt
