#  Project:      dfe-engine
#  File:         bootstrap.py
#  Purpose:      Apply the core DFE schema on daemon startup
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Apply the core DFE ClickHouse schema when the daemon starts.

The work itself lives in :mod:`dfe_engine.schema.core_schema`, which the
``dfe-schema`` binary calls too -- this module is only the daemon's way in, and
its posture: best-effort, so a briefly-unavailable ClickHouse logs an error and
startup continues rather than crash-looping the app. A deployment that wants the
schema to be a GATE runs ``dfe-schema apply`` in a wave ahead of the app, where a
failure is meant to stop things.

Disable with ``DFE_CLICKHOUSE_BOOTSTRAP_TABLES=false``.
"""

from __future__ import annotations

from scalo.logger import logger

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.applier import log_report
from dfe_engine.schema.core_schema import (
    CoreSchemaTargets,
    apply_core_schema,
    apply_query_log_archive,
)
from dfe_engine.settings import DFESettings, get_clickhouse_config


def bootstrap_clickhouse(*, settings: DFESettings) -> None:
    """Create or reconcile the core tables if the deployment asks for it."""
    if not (settings.clickhouse.bootstrap_tables):
        logger.info("ClickHouse table bootstrap disabled; skipping")
        return

    targets = CoreSchemaTargets.from_settings(settings)

    try:
        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        client = manager.get_clickhouse_client()
        report = apply_core_schema(client, targets, topology_setting=settings.clickhouse.topology)
        log_report(report, prefix="bootstrap")
    except Exception as exc:
        logger.error(f"ClickHouse bootstrap failed for database {targets.database!r}: {exc}")
        return

    # Independent of the core tables: a server with query logging disabled never
    # materialises system.query_log, and nothing in DFE fails without the cost
    # leaderboard, so it carries its own guard.
    apply_query_log_archive(client, targets)
