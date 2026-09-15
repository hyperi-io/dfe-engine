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

from dataclasses import replace
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.gitcrud.retention import effective_default_ttl_days
from dfe_engine.schema.applier import log_report
from dfe_engine.schema.core_schema import (
    CoreSchemaTargets,
    apply_core_schema,
    apply_query_log_archive,
)
from dfe_engine.settings import DFESettings, get_clickhouse_config

if TYPE_CHECKING:
    from dfe_engine.gitcrud import GitCrud


def bootstrap_clickhouse(*, settings: DFESettings, gitcrud: GitCrud | None = None) -> bool:
    """Create or reconcile the core tables if the deployment asks for it.

    Returns whether the core tables are now known to exist. False covers both a
    deployment that switched the bootstrap off and one whose ClickHouse could not
    be reached, so a caller must not report a core table as deployed on it.
    """
    if not (settings.clickhouse.bootstrap_tables):
        logger.info("ClickHouse table bootstrap disabled; skipping")
        return False

    # The console override in the deploy repo wins over the env default.
    targets = replace(
        CoreSchemaTargets.from_settings(settings),
        default_ttl_days=effective_default_ttl_days(settings, gitcrud),
    )

    try:
        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        client = manager.get_clickhouse_client()
        report = apply_core_schema(client, targets, topology_setting=settings.clickhouse.topology)
        log_report(report, prefix="bootstrap")
    except Exception as exc:
        logger.error(f"ClickHouse bootstrap failed for database {targets.database!r}: {exc}")
        return False

    # Independent of the core tables: a server with query logging disabled never
    # materialises system.query_log, and nothing in DFE fails without the cost
    # leaderboard, so it carries its own guard.
    apply_query_log_archive(client, targets)
    return True
