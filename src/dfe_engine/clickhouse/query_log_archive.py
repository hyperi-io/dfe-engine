#  Project:      dfe-engine
#  File:         clickhouse/query_log_archive.py
#  Purpose:      system.query_log -> a DFE cost/attribution archive (cost leaderboard)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Persist ``system.query_log`` into a DFE archive, parsed by attribution.

Every DFE query already carries a ``log_comment`` JSON of :class:`DfeQueryTags`
(tenant / user / feature / kind / id). ClickHouse records that verbatim in
``system.query_log`` alongside the real cost columns (read rows/bytes, duration,
memory). A materialised view lifts those rows - the moment CH flushes them - into
``dfe_audit.query_log_archive``, exploding the ``log_comment`` JSON into typed,
queryable columns. That table is the SSoT the cost views read: it DIRECTLY
unblocks the parked hunt-cost leaderboard (which was blocked on
"a worker-written real table"; see [[project_clickhouse_cloud_portability]]).

``system.query_log`` is per-node and append-only, so the MV reads each node's
local log and writes to the (topology-resolved) target - the engine form comes
from the sensing resolver, never a hardcoded literal. Attribution pattern +
the query_log_archive idea are from PostHog (MIT) - see THIRD-PARTY-NOTICES.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.schema.engine_resolver import EngineResolver, EngineSpec, ResolvedEngine
from dfe_engine.settings import get_settings

from .names import DFE_AUDIT, QUERY_LOG_ARCHIVE

_ARCHIVE_MV = f"{QUERY_LOG_ARCHIVE}_mv"
_DEFAULT_TTL_DAYS = 30


def render_ddl(
    engine: ResolvedEngine | None = None,
    *,
    ttl_days: int = _DEFAULT_TTL_DAYS,
    database: str = DFE_AUDIT,
) -> list[str]:
    """DDL to create the archive DB + table + the MV over ``system.query_log``.

    ``engine`` is a :class:`ResolvedEngine` from the sensing resolver (single ->
    MergeTree, cluster -> ReplicatedMergeTree + ON CLUSTER, Cloud -> Shared auto).
    None yields the single-node plain form (the resolver's own terminal default),
    for the no-client render path. ``database`` defaults to the canonical
    ``dfe_audit`` - override only for an isolated test target. Idempotent
    (``IF NOT EXISTS``).
    """
    on_cluster = engine.on_cluster if engine is not None else ""
    clause = engine.clause if engine is not None else "MergeTree()"
    tbl = f"{database}.{QUERY_LOG_ARCHIVE}"
    mv = f"{database}.{_ARCHIVE_MV}"
    return [
        f"CREATE DATABASE IF NOT EXISTS {database}{on_cluster}",
        (
            f"CREATE TABLE IF NOT EXISTS {tbl}{on_cluster} (\n"
            "    event_time DateTime,\n"
            "    query_id String,\n"
            "    query_duration_ms UInt64,\n"
            "    read_rows UInt64,\n"
            "    read_bytes UInt64,\n"
            "    result_rows UInt64,\n"
            "    memory_usage UInt64,\n"
            "    query_kind LowCardinality(String),\n"
            "    ch_user LowCardinality(String),\n"
            "    service LowCardinality(String),\n"
            "    tenant_id String,\n"
            "    feature LowCardinality(String),\n"
            "    kind LowCardinality(String),\n"
            "    dfe_id String,\n"
            "    trace_id String,\n"
            "    log_comment String\n"
            f") ENGINE = {clause}\n"
            "ORDER BY (event_time, query_id)\n"
            f"TTL event_time + INTERVAL {ttl_days} DAY"
        ),
        (
            f"CREATE MATERIALIZED VIEW IF NOT EXISTS {mv}{on_cluster} TO {tbl} AS\n"
            "SELECT\n"
            "    event_time,\n"
            "    query_id,\n"
            "    query_duration_ms,\n"
            "    read_rows,\n"
            "    read_bytes,\n"
            "    result_rows,\n"
            "    memory_usage,\n"
            "    query_kind,\n"
            "    user AS ch_user,\n"
            "    JSONExtractString(log_comment, 'service') AS service,\n"
            "    JSONExtractString(log_comment, 'tenant_id') AS tenant_id,\n"
            "    JSONExtractString(log_comment, 'feature') AS feature,\n"
            "    JSONExtractString(log_comment, 'kind') AS kind,\n"
            "    JSONExtractString(log_comment, 'id') AS dfe_id,\n"
            "    JSONExtractString(log_comment, 'trace_id') AS trace_id,\n"
            "    log_comment\n"
            "FROM system.query_log\n"
            # Only finished, DFE-tagged, valid-JSON rows - keeps the archive small
            # and relevant (untagged CH-internal queries are skipped).
            "WHERE type = 'QueryFinish' AND log_comment != '' AND isValidJSON(log_comment)"
        ),
    ]


def ensure(wrapper: Any, *, ttl_days: int = _DEFAULT_TTL_DAYS, database: str = DFE_AUDIT) -> None:
    """Create the archive DB + table + MV if absent (idempotent).

    ``wrapper`` is a :class:`ClickHouseClientWrapper`. The engine is resolved via
    the SAME topology config-override the data tables use
    (``settings.clickhouse.topology``: single -> MergeTree, replicated ->
    Replicated + ON CLUSTER) - NOT by independently sensing the live cluster - so
    the archive is created with the same engine form as everything else and never
    lands ON CLUSTER when the deployment is configured single. Safe to call every
    startup.

    ``system.query_log`` is created LAZILY - the server only materialises it on the
    first log flush - so on a freshly-started server the MV's source table does not
    exist yet and ``CREATE MATERIALIZED VIEW ... FROM system.query_log`` would fail
    (code 60). Flush first to materialise it (the engine has already run sensing /
    ping queries, so there is something to flush). If query logging is DISABLED in
    the server (``log_queries=0``) the source never appears and the create surfaces
    that as a real error - the archive genuinely cannot work without query logging.
    """
    engine = EngineResolver(override=get_settings().clickhouse.topology).resolve(
        EngineSpec("MergeTree"), database
    )
    wrapper.command("SYSTEM FLUSH LOGS")
    for stmt in render_ddl(engine, ttl_days=ttl_days, database=database):
        wrapper.command(stmt)


def cost_leaderboard(
    wrapper: Any,
    *,
    feature: str = "hunts",
    days: int = 7,
    limit: int = 50,
    database: str = DFE_AUDIT,
) -> list[dict[str, Any]]:
    """Top cost consumers over the archive window - the hunt-cost leaderboard.

    Groups by the attribution ``id`` (hunt id for ``feature='hunts'``) and returns
    query count + summed read rows/bytes + duration + peak memory, heaviest first.
    All bind parameters are server-side (``{name:Type}``) - never string-formatted.
    """
    sql = (
        "SELECT dfe_id AS id, feature, tenant_id, "
        "count() AS queries, sum(read_rows) AS read_rows, sum(read_bytes) AS read_bytes, "
        "sum(query_duration_ms) AS duration_ms, max(memory_usage) AS peak_memory "
        f"FROM {database}.{QUERY_LOG_ARCHIVE} "
        "WHERE event_time >= now() - toIntervalDay({days:UInt32}) "
        "AND feature = {feature:String} AND dfe_id != '' "
        "GROUP BY id, feature, tenant_id "
        "ORDER BY read_bytes DESC "
        "LIMIT {limit:UInt32}"
    )
    columns, rows = wrapper.query_rows(
        sql, parameters={"days": days, "feature": feature, "limit": limit}
    )
    return [dict(zip(columns, row, strict=False)) for row in rows]
