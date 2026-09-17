#  Project:      dfe-engine
#  File:         clickhouse/query_log_archive.py
#  Purpose:      Read the query-cost archive the schema phase stands up
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Read ``query_log_archive``, the attribution-parsed copy of ``system.query_log``.

Every DFE query carries a ``log_comment`` JSON of :class:`DfeQueryTags` (tenant /
user / feature / kind / id). ClickHouse records that verbatim in
``system.query_log`` alongside the real cost columns, and a materialised view
lifts those rows into the archive with the JSON exploded into typed columns. That
table is the SSoT the cost views read, and it unblocks the hunt-cost leaderboard.

The table and its view are declared in dfe-schemas and applied by the engine's
schema phase like every other object. They are marked OPTIONAL there: a server
with query logging disabled never materialises ``system.query_log``, and no part
of DFE fails without the cost leaderboard.

Attribution pattern + the query_log_archive idea are from PostHog (MIT) -- see
THIRD-PARTY-NOTICES.
"""

from __future__ import annotations

from typing import Any

from dfe_engine.settings import default_data_database

# The fallback for a caller with no settings; live callers pass
# clickhouse.effective_data_database, which is the SSoT for this name.
DFE_DATABASE = default_data_database()

# The archive table, by manifest id. The NAME comes off the rendered object.
ARCHIVE_ID = "data.query_log_archive"


def archive_table(database: str = DFE_DATABASE) -> str:
    """The archive table's name, as the manifest declares it."""
    from dfe_engine.schema.plan import object_names

    return object_names(ARCHIVE_ID, data_database=database)[ARCHIVE_ID]


def flush_logs(wrapper: Any) -> None:
    """Materialise ``system.query_log`` so the archive's view has a source.

    ClickHouse creates that table LAZILY, on the first log flush, so on a freshly
    started server the view's source does not exist yet and the CREATE fails with
    code 60. The engine has already run sensing and ping queries by the time the
    schema phase runs, so there is something to flush.
    """
    wrapper.command("SYSTEM FLUSH LOGS")


def cost_leaderboard(
    wrapper: Any,
    *,
    feature: str = "hunts",
    days: int = 7,
    limit: int = 50,
    database: str = DFE_DATABASE,
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
        f"FROM {database}.{archive_table(database)} "
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
