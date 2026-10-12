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

Pinning the table defaults onto chosen sources moves their deployed tables through
:func:`apply_pinned_defaults`, which uses the same per-table apply as the source pass.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from scalo.logger import logger

from dfe_engine import __version__ as engine_version
from dfe_engine.gitcrud.retention import with_default_ttl_days
from dfe_engine.schema.applier import (
    ApplyReport,
    LiveTable,
    SchemaApplier,
    SchemaApplyError,
    TableChange,
    live_tables,
)
from dfe_engine.schema.derived_registry import derived_reference_root
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.manifest_applier import ManifestApplier, declared_ttl_days
from dfe_engine.schema.plan import build_plan
from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerationError
from dfe_engine.schema.schema_loader import SchemaLoadError
from dfe_engine.source.deployment import SourceDeploymentStore
from dfe_engine.source.models import SchemaColumn, Source
from dfe_engine.source.type_registry import TypeRegistry, TypeRegistryError

LiveStatus = Literal["altered", "unchanged", "not_deployed", "failed"]

# A table's engine is fixed when it is created; changing it means copying the data out.
ENGINE_NEEDS_REBUILD = "needs a table rebuild; applies to new tables only"

# An outcome's reason reaches API callers, so ClickHouse's own text stays in the log.
CLICKHOUSE_REFUSED = "ClickHouse could not be read or refused the change; the engine log has why"

# What one source's table can refuse: the build, its types, its DDL or the server.
_TABLE_ERRORS = (
    SchemaApplyError,
    SchemaBuildError,
    SchemaLoadError,
    TypeRegistryError,
    DDLGenerationError,
    ValueError,
)


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


class TableApply(NamedTuple):
    """One source table brought to a version: the change, and what it was built from."""

    change: TableChange
    columns: list[SchemaColumn]
    config: DDLConfig


class SourceTables:
    """Brings deployed source tables to a source version's columns and TTL.

    One per pass: the resolver senses the topology once, and :attr:`applier`
    records every change in its report.
    """

    def __init__(self, client: Any, *, settings: Any) -> None:
        self.client = client
        self.database = settings.clickhouse.effective_data_database
        resolver = EngineResolver(client=client, topology_setting=settings.clickhouse.topology)
        self.applier = SchemaApplier(client, resolver)
        # Header defaults are not passed. ensure_table adds any column this build says
        # is missing, and the admin's common-header override is inherited on the next
        # deploy rather than written onto live tables from a retention reconcile.
        self._builder = SchemaBuilderV2(
            TypeRegistry.default(),
            schemas_base_dir=settings.schemas.schemas_dir or None,
            derived_base_dir=derived_reference_root(settings),
            default_engine=settings.clickhouse.default_engine,
            default_ttl_days=settings.clickhouse.default_ttl_days,
            resolver=resolver,
        )

    def apply(self, source: Source, version: str) -> TableApply:
        """Add the columns *version* says the table lacks, and move its TTL to *version*'s.

        Nothing is dropped or retyped, and the engine is left as created.

        Raises:
            SchemaApplyError: ClickHouse refused a statement or could not be read.
            SchemaBuildError: The version does not build.
            SchemaLoadError: A schema file the version names does not load.
            TypeRegistryError: A column's type is not in the type registry.
            DDLGenerationError: A name or declaration cannot be rendered as DDL.
        """
        # Built here, not read from the stored artefact: that keeps no columns, and
        # the TTL cannot be placed without the column it is declared over.
        result = self._builder.build_for_source_version(source, source_version=version)
        cfg = self._builder.build_ddl_config_for_version(source, version)
        change = self.applier.ensure_table(self.database, source.table_name, result.columns, cfg)
        return TableApply(change=change, columns=result.columns, config=cfg)

    def header_nullability_kept(
        self, source: Source, version: str, columns: list[SchemaColumn]
    ) -> tuple[str, ...]:
        """Header columns on the table whose nullability differs from *version*'s header.

        Raises:
            SchemaApplyError: ClickHouse could not be read.
            SchemaBuildError: The header profile does not load.
            TypeRegistryError: A header column's type is not in the type registry.
        """
        profile = self._builder.load_profile_for_snapshot(source.source, source.versions[version])
        names = {col.name for col in profile}
        header = [col for col in columns if col.name in names]
        return self.applier.nullability_mismatches(self.database, source.table_name, header)


def reconcile_source_ttls(
    client: Any, *, settings: Any, sources: list[Source]
) -> RetentionReconcile:
    """Apply every deployed source's table under the deployment default TTL.

    One source failing is logged and counted as skipped; it never stops the rest.
    """
    tables = SourceTables(client, settings=settings)
    database = tables.database
    outcome = RetentionReconcile(report=tables.applier.report)
    store = SourceDeploymentStore.from_settings(settings)
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
            if not (tables.applier.table_exists(database, table)):
                logger.warning(f"{database}.{table}: absent, TTL left to the next deploy")
                outcome.sources_skipped += 1
                continue
            tables.apply(source, version)
        except _TABLE_ERRORS as exc:
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


@dataclass(frozen=True, slots=True)
class FieldNotApplied:
    """A pinned value the deployed table did not take, and why."""

    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourceLiveOutcome:
    """What bringing one source's deployed table to its pinned defaults did.

    Attributes:
        source: The source name.
        status: ``altered`` or ``unchanged`` once the table was reconciled;
            ``not_deployed`` when no table runs the pinned version; ``failed`` when
            ClickHouse or the build refused.
        table: ``database.table``; empty when the source has never been deployed.
        ttl: The TTL move in days, such as ``90 -> 91``; empty when it did not move.
        columns_added: Columns the table gained.
        not_applied: Pinned values the table did not take.
        reason: Why nothing reached the table, for ``not_deployed`` and ``failed``.
    """

    source: str
    status: LiveStatus
    table: str = ""
    ttl: str = ""
    columns_added: tuple[str, ...] = ()
    not_applied: tuple[FieldNotApplied, ...] = ()
    reason: str = ""


def apply_pinned_defaults(
    connect: Callable[[], Any], *, settings: Any, sources: list[Source]
) -> list[SourceLiveOutcome]:
    """Bring each source's deployed table to the TTL and header its current version pins.

    Only a source whose current version is the deployed one has a table running
    those pins. Its table gains the columns the version adds and moves to the
    version's TTL; the engine stays as created and existing columns keep their
    type, so each outcome names what the table did not take. *connect* is called
    only when some source has such a table.

    No source raises: the pins are committed before this runs, so one table
    failing must not hide what happened to the others.

    Returns:
        One outcome per source, in the order given.
    """
    database = settings.clickhouse.effective_data_database
    outcomes: dict[str, SourceLiveOutcome] = {}
    deployed: list[Source] = []
    for source in sources:
        if source.deployed_version is None:
            outcomes[source.source] = SourceLiveOutcome(
                source=source.source,
                status="not_deployed",
                reason="never deployed; its table is created with these values",
            )
        elif source.deployed_version != source.current:
            outcomes[source.source] = SourceLiveOutcome(
                source=source.source,
                status="not_deployed",
                table=f"{database}.{source.table_name}",
                reason=(
                    f"version {source.current} carries these values and version "
                    f"{source.deployed_version} is deployed; deploying {source.current} "
                    "applies them"
                ),
            )
        else:
            deployed.append(source)
    if deployed:
        for outcome in _apply_deployed(connect, settings=settings, sources=deployed):
            outcomes[outcome.source] = outcome
    return [outcomes[source.source] for source in sources]


def _apply_deployed(
    connect: Callable[[], Any], *, settings: Any, sources: list[Source]
) -> list[SourceLiveOutcome]:
    database = settings.clickhouse.effective_data_database
    try:
        tables = SourceTables(connect(), settings=settings)
        before = live_tables(tables.client, database)
    # The pins are committed, so a ClickHouse that cannot be reached is reported per source.
    except Exception as exc:
        logger.warning(f"table defaults not applied to the deployed tables: {exc}")
        return [
            SourceLiveOutcome(
                source=source.source,
                status="failed",
                table=f"{database}.{source.table_name}",
                reason=CLICKHOUSE_REFUSED,
            )
            for source in sources
        ]
    return [_apply_one(tables, source, before.get(source.table_name)) for source in sources]


def _apply_one(tables: SourceTables, source: Source, live: LiveTable | None) -> SourceLiveOutcome:
    target = f"{tables.database}.{source.table_name}"
    if live is None:
        return SourceLiveOutcome(
            source=source.source,
            status="not_deployed",
            table=target,
            reason="the table is not in ClickHouse; the next deploy creates it with these values",
        )
    try:
        applied = tables.apply(source, source.current)
        kept = tables.header_nullability_kept(source, source.current, applied.columns)
    except _TABLE_ERRORS as exc:
        logger.warning(f"{target}: table defaults not applied: {exc}")
        # The build, type and DDL errors are about the source's own schema; the server's is not.
        reason = CLICKHOUSE_REFUSED if isinstance(exc, SchemaApplyError) else str(exc)
        return SourceLiveOutcome(source=source.source, status="failed", table=target, reason=reason)
    not_applied: list[FieldNotApplied] = []
    if applied.change.ttl_skipped:
        not_applied.append(FieldNotApplied("ttl_days", applied.change.ttl_skipped))
    if live.variant != parse_engine(applied.config.engine).variant:
        not_applied.append(FieldNotApplied("engine", ENGINE_NEEDS_REBUILD))
    if kept:
        not_applied.append(
            FieldNotApplied(
                "common_header_version",
                f"existing columns keep their nullability: {', '.join(kept)}",
            )
        )
    return SourceLiveOutcome(
        source=source.source,
        status="altered" if applied.change.action == "altered" else "unchanged",
        table=target,
        ttl=applied.change.ttl,
        columns_added=applied.change.columns_added,
        not_applied=tuple(not_applied),
    )
