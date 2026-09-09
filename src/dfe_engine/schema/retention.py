#  Project:      dfe-engine
#  File:         schema/retention.py
#  Purpose:      Reconcile every table that follows the default TTL, straight away
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bring the live tables to a changed default TTL without waiting for a deploy.

The core tables go through :func:`~dfe_engine.schema.core_schema.apply_core_schema`
with the new default. Each deployed source's table goes through the same
:class:`~dfe_engine.schema.applier.SchemaApplier` with the DDL config its
deployed version builds under the new default, so a source that declares its own
``ttl_days`` keeps it and one that does not follows the default.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from scalo.logger import logger

from dfe_engine.schema.applier import ApplyReport, SchemaApplier
from dfe_engine.schema.core_schema import CoreSchemaTargets, apply_core_schema
from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
from dfe_engine.source.deployment import SourceDeploymentStore, ensure_build_artifact
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry


@dataclass
class RetentionReconcile:
    """What one default-TTL reconcile did."""

    report: ApplyReport
    # Deployed sources whose table was reconciled.
    sources_reconciled: int = 0
    # Deployed sources left to their next deploy: table absent or build failed.
    sources_skipped: int = 0


def reconcile_default_ttl(
    client: Any, *, settings: Any, sources: list[Source], days: int
) -> RetentionReconcile:
    """Apply the core schema and every deployed source's table under *days*.

    Raises:
        SchemaApplyError: ClickHouse rejected a statement or could not be read.
    """
    targets = replace(CoreSchemaTargets.from_settings(settings), default_ttl_days=days or None)
    resolver = EngineResolver(client=client, topology_setting=settings.clickhouse.topology)
    report = apply_core_schema(client, targets, resolver=resolver)
    outcome = RetentionReconcile(report=report)

    applier = SchemaApplier(client, resolver)
    applier.report = report
    store = SourceDeploymentStore.from_settings(settings)
    schemas_dir = settings.schemas.schemas_dir or None
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=schemas_dir,
        default_engine=settings.clickhouse.default_engine,
        default_ttl_days=days,
        resolver=resolver,
    )
    for source in sources:
        doc = store.load_deploy_document(source.source)
        version = doc.deployed_version if doc is not None else None
        if not version or version not in source.versions:
            continue
        table = source.table_name
        if not applier.table_exists(targets.database, table):
            logger.warning(f"{targets.database}.{table}: absent, TTL left to the next deploy")
            outcome.sources_skipped += 1
            continue
        try:
            result, _artifact = ensure_build_artifact(
                store, source, version_id=version, schemas_base_dir=schemas_dir, resolver=resolver
            )
            cfg = builder.build_ddl_config_for_version(source, version)
        except SchemaBuildError as exc:
            logger.warning(f"{targets.database}.{table}: TTL left to the next deploy: {exc}")
            outcome.sources_skipped += 1
            continue
        applier.ensure_table(targets.database, table, result.columns, cfg)
        outcome.sources_reconciled += 1
    return outcome
