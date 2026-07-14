#  Project:      dfe-engine
#  File:         bootstrap.py
#  Purpose:      Create the DFE database and its bootstrap tables on startup
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Create the DFE database, landing table, and hunt results table in ClickHouse.

Runs once at startup. The database name comes from ``clickhouse.effective_data_database`` (default ``dfe``) and the landing table columns from ``clickhouse.default_table_profile`` (default ``timeseries``). Best-effort: a failure is logged and startup continues, so a briefly-unavailable ClickHouse does not crash-loop the app. Disable with ``DFE_CLICKHOUSE_BOOTSTRAP_TABLES=false``.
"""

from __future__ import annotations

from scalo.logger import logger

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.settings import DFESettings, get_clickhouse_config


def bootstrap_clickhouse(*, settings: DFESettings) -> None:
    """Create the DFE database and its bootstrap tables if they do not yet exist."""
    if not (settings.clickhouse.bootstrap_tables):
        logger.info("ClickHouse table bootstrap disabled; skipping")
        return

    database = settings.clickhouse.effective_data_database
    profile = settings.clickhouse.default_table_profile

    try:
        writer = DDLFileWriter()
        default_ddl = writer.generate_default_table(profile_name=profile).replace("{db}", database)
        hunt_results_ddl = writer.generate_hunt_results_table(profile_name=profile).replace(
            "{db}", database
        )

        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        client = manager.get_clickhouse_client()
        client.execute(f"CREATE DATABASE IF NOT EXISTS {database}")
        client.execute(default_ddl)
        client.execute(hunt_results_ddl)
        logger.info(
            f"Bootstrapped ClickHouse database {database!r} "
            f"(default + hunt_results tables, profile {profile!r})"
        )
    except Exception as exc:
        logger.error(f"ClickHouse bootstrap failed for database {database!r}: {exc}")

    # Best-effort: stand up the query-log cost/attribution archive MV (the cost
    # leaderboard source over system.query_log). Independent of the core table
    # bootstrap above - a CH with query logging disabled must not fail startup, so
    # it carries its own guard.
    try:
        from dfe_engine.clickhouse import query_log_archive

        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        query_log_archive.ensure(manager.get_clickhouse_client())
        logger.info("Ensured query_log_archive cost/attribution MV")
    except Exception as exc:
        logger.warning(f"query_log_archive bootstrap skipped: {exc}")
