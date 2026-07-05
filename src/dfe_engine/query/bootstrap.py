#  Project:      dfe-engine
#  File:         src/dfe_engine/query/bootstrap.py
#  Purpose:      Compose the ViewExecutor from settings (lifespan + client)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build a ViewExecutor from DFE settings.

One composition shared by the API lifespan and QueryClient's direct mode: a
ClickHouseAdapter bound to the settings-derived connection config, the
restricted query_reader client for execution, the admin client for catalog
discovery, and (optionally) the builtin-view DDL bootstrap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scalo.logger import logger

if TYPE_CHECKING:
    from dfe_engine.query.executor import ViewExecutor
    from dfe_engine.settings import DFESettings


def build_view_executor(settings: DFESettings, *, auto_bootstrap: bool = False) -> ViewExecutor:
    """Compose a ViewExecutor from settings. Raises when ClickHouse is unreachable.

    Args:
        settings: DFE settings (clickhouse + query_views sections).
        auto_bootstrap: Apply the builtin .sql views before returning (the
            lifespan passes settings.query_views.auto_bootstrap). DDL failures
            are logged, not raised - the executor still serves live views.

    Returns:
        A ready ViewExecutor over the restricted query_reader connection.
    """
    from dfe_engine.query.catalog import ViewCatalog
    from dfe_engine.query.datasources.clickhouse import ClickHouseAdapter
    from dfe_engine.query.ddl import DDLManager
    from dfe_engine.query.executor import ViewExecutor
    from dfe_engine.settings import get_clickhouse_config

    qv = settings.query_views

    # `target` selects the connection (auth db); catalog/executor qualify view
    # + table lookups against the data database. The explicit config binds the
    # ClickHouseManager singleton to DFE_CLICKHOUSE_*, not localhost defaults.
    adapter = ClickHouseAdapter(
        target=settings.clickhouse.database,
        config=get_clickhouse_config(settings),
    )
    restricted_client = adapter.get_restricted_client()
    admin_client = adapter.manager.get_clickhouse_client()
    data_db = settings.clickhouse.effective_data_database

    if auto_bootstrap:
        # apply_all_builtin_views logs per-view failures itself; this guard is
        # for anything outside that loop so bootstrap never sinks the executor.
        try:
            DDLManager(admin_client, database=data_db).apply_all_builtin_views()
        except Exception:
            logger.exception("Builtin-view bootstrap failed; serving existing views only")

    catalog = ViewCatalog(
        client=admin_client,
        database=data_db,
        cache_ttl=qv.catalog_cache_ttl,
        view_prefix=qv.view_prefix,
    )

    return ViewExecutor(
        restricted_client=restricted_client,
        catalog=catalog,
        database=data_db,
        default_limit=qv.default_limit,
        max_limit=qv.max_limit,
        default_timeout=qv.default_timeout,
        max_timeout=qv.max_timeout,
    )
