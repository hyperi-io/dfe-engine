#  Project:      dfe-engine
#  File:         schema/retention.py
#  Purpose:      Bring every table that follows the default TTL to the default
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Reconcile the tables that follow the deployment default TTL.

Two kinds of table follow it. A core table does when its dfe-schemas definition
names TTL columns and no ``ttl_days``; the schema phase creates it with the
default and refuses a later change as drift, so a change reaches it only through
:func:`reconcile_core_ttls`. A deployed source's table does when its schema
leaves ``ttl_days`` unset, and :func:`reconcile_source_ttls` applies it through
the same :class:`~dfe_engine.schema.applier.SchemaApplier` a deploy uses.

The engine runs the source pass on every start. An admin's change of the default
runs both, through :func:`reconcile_default_ttl`. The default each pass applies is
``settings.clickhouse.default_ttl_days``, so a caller hands in settings carrying the
effective value (:func:`dfe_engine.gitcrud.retention.effective_settings`).
"""

from dataclasses import dataclass, field
from typing import Any

from scalo.logger import logger

from dfe_engine import __version__ as engine_version
from dfe_engine.gitcrud.retention import with_default_ttl_days
from dfe_engine.schema.applier import ApplyReport, SchemaApplier, SchemaApplyError
from dfe_engine.schema.derived_registry import derived_reference_root
from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.manifest_applier import ManifestApplier, declared_ttl_days
from dfe_engine.schema.plan import build_plan
from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
from dfe_engine.schema.schema_loader import SchemaLoadError
from dfe_engine.source.deployment import SourceDeploymentStore
from dfe_engine.source.models import Source
from dfe_engine.source.type_registry import TypeRegistry


@dataclass
class RetentionReconcile:
    """What one default-TTL reconcile did."""

    report: ApplyReport = field(default_factory=ApplyReport)
    # "database.table: <live> -> <new>" for each core table whose TTL changed.
    core_altered: list[str] = field(default_factory=list)
    # Deployed sources whose table was reconciled.
    sources_reconciled: int = 0
    # Deployed sources left to their next deploy: table absent, build failed or ClickHouse refused.
    sources_skipped: int = 0


def default_followers(settings: Any) -> frozenset[str]:
    """Manifest ids of the core tables whose TTL is the deployment default.

    Rendered twice, without a server: a follower carries a TTL only when a default
    is given, while a table that declares its own renders it either way.
    """
    probe = build_plan(settings=with_default_ttl_days(settings, 1))
    unset = build_plan(settings=with_default_ttl_days(settings, 0))
    no_own_ttl = {
        obj.id
        for obj in unset.tables()
        if obj.kind == "table" and declared_ttl_days(obj.statements[0]) is None
    }
    return frozenset(
        obj.id
        for obj in probe.tables()
        if obj.id in no_own_ttl and declared_ttl_days(obj.statements[0]) is not None
    )


def reconcile_core_ttls(client: Any, *, settings: Any) -> list[str]:
    """Bring each core table that follows the default TTL to it, and change nothing else.

    Returns:
        ``database.table: <live> -> <new>`` for each table altered. An absent table
        is left for the schema phase to create.

    Raises:
        ManifestApplyError: ClickHouse refused a statement or could not be read.
        SchemaPlanError: The manifest could not be rendered.
    """
    followers = default_followers(settings)
    plan = build_plan(settings=settings, client=client)
    applier = ManifestApplier(
        client,
        ledger=None,
        schemas_version=plan.schemas_version,
        engine_version=engine_version,
        topology=plan.topology,
    )
    altered: list[str] = []
    for rendered in plan.tables():
        if rendered.id not in followers:
            continue
        move = applier.reconcile_ttl(rendered)
        if move:
            logger.info(f"{rendered.database}.{rendered.name}: default TTL {move} days")
            altered.append(f"{rendered.database}.{rendered.name}: {move}")
    return altered


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
        derived_base_dir=derived_reference_root(settings),
        default_engine=settings.clickhouse.default_engine,
        default_ttl_days=settings.clickhouse.default_ttl_days,
        resolver=resolver,
    )
    for source in sources:
        # reconcile_core_ttls and the schema phase own the engine's own tables.
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
            # Built here, not read from the stored artefact: that keeps no columns, and
            # the TTL cannot be placed without the column it is declared over.
            result = builder.build_for_source_version(source, source_version=version)
            cfg = builder.build_ddl_config_for_version(source, version)
            applier.ensure_table(database, table, result.columns, cfg)
        except (SchemaApplyError, SchemaBuildError, SchemaLoadError) as exc:
            logger.warning(f"{database}.{table}: TTL left to the next deploy: {exc}")
            outcome.sources_skipped += 1
            continue
        outcome.sources_reconciled += 1
    return outcome


def reconcile_default_ttl(
    client: Any, *, settings: Any, sources: list[Source]
) -> RetentionReconcile:
    """Bring every table that follows the default TTL to it: the core tables, then the sources.

    Raises:
        ManifestApplyError: ClickHouse refused a core-table statement or could not be read.
        SchemaPlanError: The manifest could not be rendered.
    """
    core_altered = reconcile_core_ttls(client, settings=settings)
    outcome = reconcile_source_ttls(client, settings=settings, sources=sources)
    outcome.core_altered = core_altered
    return outcome
