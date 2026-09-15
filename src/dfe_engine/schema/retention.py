#  Project:      dfe-engine
#  File:         schema/retention.py
#  Purpose:      Bring every deployed source's table to the deployment default TTL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Reconcile deployed source tables against ``DFE_CLICKHOUSE_DEFAULT_TTL_DAYS``.

The core tables follow the default through
:func:`~dfe_engine.schema.core_schema.apply_core_schema`. A deployed source's
table goes through the same :class:`~dfe_engine.schema.applier.SchemaApplier`
with the DDL config its deployed version builds under the default, so a source
that declares its own ``ttl_days`` keeps it and one that does not follows the
default. The engine runs this at startup, so a changed default reaches every
table on the next restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scalo.logger import logger

from dfe_engine.schema.applier import ApplyReport, SchemaApplier, SchemaApplyError
from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
from dfe_engine.source.deployment import SourceDeploymentStore, ensure_build_artifact
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry


@dataclass
class RetentionReconcile:
    """What one source TTL reconcile did."""

    report: ApplyReport = field(default_factory=ApplyReport)
    # Deployed sources whose table was reconciled.
    sources_reconciled: int = 0
    # Deployed sources left to their next deploy: table absent, build failed or ClickHouse refused.
    sources_skipped: int = 0


def reconcile_source_ttls(
    client: Any, *, settings: Any, sources: list[Source]
) -> RetentionReconcile:
    """Apply every deployed source's table under the deployment default TTL.

    One source failing is logged and counted as skipped; it never stops the rest.
    """
    database = settings.clickhouse.effective_data_database
    resolver = EngineResolver(client=client, topology_setting=settings.clickhouse.topology)
    applier = SchemaApplier(client, resolver)
    outcome = RetentionReconcile(report=applier.report)
    store = SourceDeploymentStore.from_settings(settings)
    schemas_dir = settings.schemas.schemas_dir or None
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=schemas_dir,
        default_engine=settings.clickhouse.default_engine,
        default_ttl_days=settings.clickhouse.default_ttl_days,
        resolver=resolver,
    )
    for source in sources:
        # apply_core_schema owns the engine's own tables, TTL included.
        if source.resource_type == "core":
            continue
        doc = store.load_deploy_document(source.source)
        version = doc.deployed_version if doc is not None else None
        if not (version) or (version not in source.versions):
            continue
        table = source.table_name
        try:
            if not (applier.table_exists(database, table)):
                logger.warning(f"{database}.{table}: absent, TTL left to the next deploy")
                outcome.sources_skipped += 1
                continue
            result, _artifact = ensure_build_artifact(
                store, source, version_id=version, schemas_base_dir=schemas_dir, resolver=resolver
            )
            cfg = builder.build_ddl_config_for_version(source, version)
            applier.ensure_table(database, table, result.columns, cfg)
        except (SchemaApplyError, SchemaBuildError) as exc:
            logger.warning(f"{database}.{table}: TTL left to the next deploy: {exc}")
            outcome.sources_skipped += 1
            continue
        outcome.sources_reconciled += 1
    return outcome
