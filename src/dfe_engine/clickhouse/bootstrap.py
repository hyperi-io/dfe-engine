#  Project:      dfe-engine
#  File:         bootstrap.py
#  Purpose:      The daemon's way into the schema bootstrap phase
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Run the schema phase on daemon startup, then reconcile the source tables' TTL.

The phase itself lives in :mod:`dfe_engine.schema.phase`, which applies the
pinned dfe-schemas manifest and is the only code path in the engine that issues
DDL for a declared object. This module is the daemon's way in and the place the
source tables are brought to the deployment default retention afterwards, so a
changed default reaches every deployed source's table on restart. The phase
refuses a changed default on a core table as TTL drift; an admin's change through
``PUT /api/v1/system/retention`` is what applies it there.

The phase is a GATE, not best-effort: its outcome decides readiness. Disable it
with ``DFE_CLICKHOUSE_BOOTSTRAP_TABLES=false``, which reports the schema state as
unknown and gates nothing.
"""

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.schema.phase import SchemaBootstrapState, run_bootstrap
from dfe_engine.settings import DFESettings, get_clickhouse_config

if TYPE_CHECKING:
    from dfe_engine.schema.metrics import SchemaMetrics
    from dfe_engine.source.models import Source


def _reconcile_sources(*, client: object, settings: DFESettings, sources: list[Source]) -> None:
    """Bring deployed source tables to the default TTL; a failure here never fails the phase."""
    from dfe_engine.schema.retention import reconcile_source_ttls

    try:
        outcome = reconcile_source_ttls(client, settings=settings, sources=sources)
    except Exception as exc:
        logger.error(f"source TTL reconcile failed: {exc}")
        return
    logger.info(f"bootstrap sources: {outcome.report.summary()}")
    if outcome.sources_skipped:
        logger.warning(
            "source TTL reconcile left sources to their next deploy",
            count=outcome.sources_skipped,
        )


def bootstrap_clickhouse(
    *,
    settings: DFESettings,
    sources: list[Source] | None = None,
    metrics: SchemaMetrics | None = None,
) -> SchemaBootstrapState:
    """Apply the manifest, then reconcile the deployed source tables' TTL.

    Returns the phase state. ``state.ready`` is what the readiness check reads,
    and ``state.converged`` is what says the objects exist -- a caller must not
    report a core table as deployed unless that one is true. ``metrics`` is where
    the phase reports its outcome; ``None`` reports nothing.
    """
    state = run_bootstrap(settings=settings, metrics=metrics)
    if not state.converged or not sources:
        return state

    try:
        manager = ClickHouseManager.get_instance(get_clickhouse_config(settings=settings))
        _reconcile_sources(
            client=manager.get_clickhouse_client(), settings=settings, sources=sources
        )
    except Exception as exc:  # the schema is applied; a TTL pass must not undo that
        logger.error(f"source TTL reconcile could not run: {exc}")
    return state
