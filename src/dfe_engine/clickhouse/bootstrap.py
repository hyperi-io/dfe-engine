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

from hyperi_pylib.logger import logger

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
            "Bootstrapped ClickHouse database %r (default + hunt_results tables, profile %r)",
            database,
            profile,
        )
    except Exception as exc:
        logger.error("ClickHouse bootstrap failed for database %r: %s", database, exc)
