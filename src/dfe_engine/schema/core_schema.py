#  Project:      dfe-engine
#  File:         schema/core_schema.py
#  Purpose:      What tables every DFE deployment needs, and applying them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The core DFE schema: the tables a deployment needs before anything streams.

One definition, three callers. dfe-infra runs it as an ArgoCD job in a wave
before the data plane, dfe-docker as a one-shot service the data plane waits on,
and the dfe-engine daemon in-process on boot -- all through
:func:`apply_core_schema`, so no target carries its own copy of the decision.

Two kinds of table meet here. The DATA tables (``default``, ``detection``,
``detection_checkpoint``) take their columns from dfe-schemas; the engine's own
state tables come from :mod:`.internal_tables`. Both resolve their storage engine
through the same sensing resolver and apply through the same
:class:`~dfe_engine.schema.applier.SchemaApplier`.

All of them land in ONE database, ``clickhouse.data_database`` (default ``dfe``),
telemetry included. Org isolation is by row policy, not by splitting databases
(see :mod:`dfe_engine.governance.ch.models`).

The SOC2 audit trail is not among them: it is structured log events through the
OTel pipeline, with no table behind it (see :mod:`dfe_engine.auth.audit`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scalo.logger import logger

from dfe_engine.schema.applier import ApplyReport, SchemaApplier
from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.internal_tables import hunt_coordination_specs, internal_specs
from dfe_engine.schema.otel_tables import TRACE_ID_TS_VIEW, otel_specs, trace_id_ts_view_ddl
from dfe_engine.schema.schema_ddl import TableSpec, with_default_ttl


@dataclass(frozen=True)
class CoreSchemaTargets:
    """Where the core schema lands: the DFE database, landing table and profile."""

    database: str
    landing_table: str = "main"
    profile: str = "timeseries"
    # Retention for a time-series table that declares none; None = no default.
    default_ttl_days: int | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> CoreSchemaTargets:
        """Read the targets off ``settings.clickhouse``."""
        return cls.from_clickhouse(settings.clickhouse)

    @classmethod
    def from_clickhouse(cls, ch: Any) -> CoreSchemaTargets:
        """Read the targets off a ``ClickHouseSettings`` directly.

        The schema tooling loads only that section, because the full settings
        model validates an API posture it does not have.
        """
        return cls(
            database=ch.effective_data_database,
            landing_table=ch.landing_table,
            profile=ch.default_table_profile,
            default_ttl_days=ch.default_ttl_days or None,
        )


def core_table_specs(
    targets: CoreSchemaTargets, *, resolver: EngineResolver | None = None
) -> list[TableSpec]:
    """Every core table, in apply order.

    The data tables lead: an operator reading the log wants the landing table
    confirmed first, and the coordination tables are useless without it.
    """
    writer = DDLFileWriter(
        resolver=resolver, database=targets.database, landing_table=targets.landing_table
    )
    ttl = targets.default_ttl_days
    # The checkpoint, internal and coordination tables are state, not time series.
    return [
        with_default_ttl(writer.default_table_spec(profile_name=targets.profile), ttl),
        with_default_ttl(writer.detection_table_spec(profile_name=targets.profile), ttl),
        writer.detection_checkpoint_table_spec(),
        *internal_specs(targets.database),
        *hunt_coordination_specs(targets.database),
        *(with_default_ttl(spec, ttl) for spec in otel_specs(targets.database)),
    ]


def landing_table_ddl(targets: CoreSchemaTargets) -> str:
    """The landing table's CREATE TABLE, exactly as ``apply_core_schema`` applies it.

    The source build path renders its own DDL from the stored schema, which is not
    the same statement: anything recording what the bootstrap deployed has to read
    the spec the bootstrap uses, not rebuild one beside it.
    """
    writer = DDLFileWriter(database=targets.database, landing_table=targets.landing_table)
    spec = with_default_ttl(
        writer.default_table_spec(profile_name=targets.profile), targets.default_ttl_days
    )
    return writer.render_spec(spec)


def apply_core_schema(
    client: Any,
    targets: CoreSchemaTargets,
    *,
    resolver: EngineResolver | None = None,
    topology_setting: str | None = None,
    dry_run: bool = False,
) -> ApplyReport:
    """Create or reconcile every core table, and report what changed.

    Args:
        client: A live ClickHouse client exposing ``command`` and ``query``.
        targets: The databases and landing profile to apply into.
        resolver: Engine resolver. Defaults to one sensing on *client*, which is
            the only configuration that emits ``ON CLUSTER``.
        topology_setting: ``DFE_CLICKHOUSE_TOPOLOGY``, used when sensing fails.
        dry_run: Collect the statements without executing them.

    Raises:
        SchemaApplyError: A statement was rejected, or the server could not be
            read. The gate callers want this to reach their exit code.
    """
    resolver = resolver or EngineResolver(client=client, topology_setting=topology_setting)
    applier = SchemaApplier(client, resolver, dry_run=dry_run)

    # The database first, and cluster-wide when sensed: ON CLUSTER table DDL
    # lands on nodes that have no database to put it in otherwise.
    applier.ensure_database(targets.database)

    for spec in core_table_specs(targets, resolver=resolver):
        applier.ensure_table(targets.database, spec.name, spec.columns, spec.config)

    # After otel_traces_trace_id_ts, which the view writes into.
    on_cluster = resolver.resolve(parse_engine("MergeTree"), targets.database).on_cluster
    applier.ensure_view(
        targets.database,
        TRACE_ID_TS_VIEW,
        trace_id_ts_view_ddl(targets.database, on_cluster),
    )

    return applier.report


def apply_query_log_archive(client: Any, targets: CoreSchemaTargets) -> bool:
    """Stand up the query-log cost archive. Returns whether it succeeded.

    Kept out of :func:`apply_core_schema` and non-fatal: it reads
    ``system.query_log``, which a server with query logging disabled never
    materialises, and no part of DFE fails without the cost leaderboard.
    """
    try:
        from dfe_engine.clickhouse import query_log_archive

        query_log_archive.ensure(client, database=targets.database)
        return True
    except Exception as exc:
        logger.warning(f"query_log_archive skipped: {exc}")
        return False
