#  Project:      dfe-engine
#  File:         schema/internal_tables.py
#  Purpose:      The engine's OWN ClickHouse tables, as specs rather than SQL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The tables dfe-engine keeps for itself: hunt coordination, alert cooldown.

Distinct from the DATA tables, whose columns come from dfe-schemas. These hold
the engine's own working state, so their columns live here in code -- the same
call the ``query_log_archive`` audit MV already makes.

The ENGINE is declared here as an INTENT and resolved per topology, never as a
literal. A literal is correct on a single node and wrong on a cluster, where the
table lands only on the replica the connection resolved to: a restart that
reconnects elsewhere finds no table, creates an empty one, and the hunt schedule
silently loses its watermark.
"""

from __future__ import annotations

from dfe_engine.schema.schema_ddl import DDLConfig, TableSpec
from dfe_engine.source.models import SchemaColumn


def _col(
    name: str,
    ch_type: str,
    *,
    order: int | None = None,
    default: str | None = None,
    codec: str | None = None,
    comment: str | None = None,
    low_cardinality: bool = False,
) -> SchemaColumn:
    """An internal column, typed by exact ClickHouse type.

    ``ch_override`` rather than a DFE primitive: these are engine state columns,
    not user data, so there is no primitive mapping to route them through. The
    override path resolves non-nullable, which is what a sorting key needs.
    ``LowCardinality`` is an attribute rather than part of the type, since the
    override catalogue does not accept the wrapped form.
    """
    return SchemaColumn(
        name=name,
        type="string",
        ch_override=ch_type,
        attribute=["lowcardinality"] if low_cardinality else [],
        order=order,
        default=default,
        codec=codec,
        comment=comment,
    )


def _internal_config(
    database: str,
    engine: str,
    *,
    ttl_days: int | None = None,
    ttl_columns: list[str] | None = None,
) -> DDLConfig:
    """DDL config for an engine-internal table.

    No projection, and no TTL unless asked: these hold one row per hunt or alert
    rule rather than a time series, so the data-table defaults would add a TTL
    clause on columns that do not exist and a projection nothing reads.
    """
    return DDLConfig(
        db=database,
        engine=engine,
        ttl_days=ttl_days,
        ttl_columns=ttl_columns or [],
        projection_order_by=None,
    )


# ── hunt coordination (data database) ───────────────────────────────
# One lease, one watermark and one state row per hunt. ReplacingMergeTree on a
# recency column, so the newest write wins per hunt_id after merge and the
# readers order explicitly rather than relying on merge state.


def hunt_lease_spec(database: str) -> TableSpec:
    """Who currently owns each hunt, and until when."""
    return TableSpec(
        name="hunt_lease",
        columns=[
            _col("hunt_id", "String", order=0),
            _col("owner", "String", comment="worker id holding the lease"),
            _col("fire", "Int64", comment="the scheduled fire time this lease covers"),
            _col("lease_until", "Int64", comment="epoch seconds the claim expires"),
            _col("claimed", "DateTime64(3)", default="now64(3)"),
        ],
        config=_internal_config(database, "ReplacingMergeTree(claimed)"),
    )


def hunt_watermark_spec(database: str) -> TableSpec:
    """How far through its window each hunt has processed."""
    return TableSpec(
        name="hunt_watermark",
        columns=[
            _col("hunt_id", "String", order=0),
            _col("watermark", "Int64", comment="epoch seconds processed up to"),
            _col("updated", "DateTime64(3)", default="now64(3)"),
        ],
        config=_internal_config(database, "ReplacingMergeTree(updated)"),
    )


def hunt_state_spec(database: str) -> TableSpec:
    """Per-hunt overrun accounting, for backing a too-aggressive schedule off."""
    return TableSpec(
        name="hunt_state",
        columns=[
            _col("hunt_id", "String", order=0),
            _col("overrun_count", "Int64"),
            _col("too_aggressive", "UInt8"),
            _col("updated", "DateTime64(3)", default="now64(3)"),
        ],
        config=_internal_config(database, "ReplacingMergeTree(updated)"),
    )


def hunt_schedule_spec(database: str) -> TableSpec:
    """The materialised hunt schedule KEDA wakes on.

    ``enabled`` is a soft tombstone: a hunt removed from gitops stops waking the
    scaler without anything having to delete a row.
    """
    return TableSpec(
        name="hunt_schedule",
        columns=[
            _col("hunt_id", "String", order=0),
            _col("interval_seconds", "Int64"),
            _col("phase_offset", "Int64"),
            _col("enabled", "UInt8", default="1", comment="soft tombstone"),
            _col("updated", "DateTime64(3)", default="now64(3)"),
        ],
        config=_internal_config(database, "ReplacingMergeTree(updated)"),
    )


def hunt_coordination_specs(database: str) -> list[TableSpec]:
    """Every hunt coordination table, in the data database."""
    return [
        hunt_lease_spec(database),
        hunt_watermark_spec(database),
        hunt_state_spec(database),
        hunt_schedule_spec(database),
    ]


# ── alert state (audit database) ────────────────────────────────────


def repository_spec(database: str) -> TableSpec:
    """The scope-aligned small-object store: UI prefs, JSON, small files.

    No ``_org_id`` column by design, which keeps the RBAC reconciler's org
    discovery away from it. Writes are always INSERTs, latest ``updated_at``
    wins, and a delete is a tombstone row.
    """
    return TableSpec(
        name="repository",
        columns=[
            _col("scope", "String", order=0, low_cardinality=True),
            _col("scope_id", "String", order=1),
            _col("namespace", "String", order=2, low_cardinality=True),
            _col("key", "String", order=3),
            _col("content_type", "String", low_cardinality=True),
            _col("value", "String", codec="ZSTD(3)"),
            _col("size", "UInt32"),
            _col("updated_by", "String"),
            _col("updated_at", "DateTime64(3)"),
            _col("is_deleted", "UInt8", default="0", comment="tombstone"),
        ],
        config=_internal_config(database, "ReplacingMergeTree(updated_at, is_deleted)"),
    )


def alert_state_spec(database: str) -> TableSpec:
    """Alert cooldown state: when each rule last fired, per org and group."""
    config = _internal_config(
        database,
        "ReplacingMergeTree(last_fired_at)",
        ttl_days=30,
        ttl_columns=["last_fired_at"],
    )
    return TableSpec(
        name="alert_state",
        columns=[
            _col("hunt_name", "String", order=0, codec="LZ4", low_cardinality=True),
            _col("rule_name", "String", order=1, codec="LZ4", low_cardinality=True),
            _col("_org_id", "String", order=2, codec="LZ4", low_cardinality=True),
            _col("group_key", "String", order=3, default="''", codec="ZSTD"),
            _col("last_fired_at", "DateTime", codec="DoubleDelta, LZ4"),
            _col("fire_count", "UInt32", default="1", codec="Delta, ZSTD"),
            _col("suppressed_count", "UInt64", default="0", codec="Delta, ZSTD"),
        ],
        config=config,
    )


def internal_specs(database: str) -> list[TableSpec]:
    """Every engine-state table that lives in the internal database."""
    return [repository_spec(database), alert_state_spec(database)]
