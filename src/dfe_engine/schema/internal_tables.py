#  Project:      dfe-engine
#  File:         schema/internal_tables.py
#  Purpose:      The engine's OWN ClickHouse tables, read from dfe-schemas
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The tables dfe-engine keeps for itself: hunt coordination, alert cooldown.

Defined in dfe-schemas under ``tables/internal/``; this module mostly only names
them. They hold the engine's own working state rather than user data, so their
columns are exact ClickHouse types with no meta-schema mapping behind them.

``hunt_run`` and ``hunt_runner_heartbeat`` are declared here in code instead. They
are the same kind of table and go through the same applier and resolver, so their
engine is still resolved per topology; they are written down here because the
runner needs them now and the schema package moves on its own release.
"""

from __future__ import annotations

from dfe_engine.schema.schema_ddl import DDLConfig, TableSpec
from dfe_engine.schema.table_loader import load_table_spec
from dfe_engine.source.models import SchemaColumn

HUNT_COORDINATION_REFS = (
    "tables/internal/hunt_lease",
    "tables/internal/hunt_watermark",
    "tables/internal/hunt_state",
    "tables/internal/hunt_schedule",
)

INTERNAL_REFS = (
    "tables/internal/repository",
    "tables/internal/alert_state",
)


def repository_spec(database: str) -> TableSpec:
    """The scope-aligned small-object store: UI prefs, JSON, small files."""
    return load_table_spec("tables/internal/repository", database)


def alert_state_spec(database: str) -> TableSpec:
    """Alert cooldown state: when each rule last fired, per org and group."""
    return load_table_spec("tables/internal/alert_state", database)


def _ch_column(name: str, ch_type: str, **kwargs) -> SchemaColumn:
    """One column typed by exact ClickHouse type, the way the tables/ loader builds them."""
    return SchemaColumn(name=name, type="string", ch_override=ch_type, **kwargs)


def hunt_run_spec(database: str) -> TableSpec:
    """One row per hunt fire: what the operator asked for, and what the run wrote.

    A ``requested`` row is an operator pressing run now; the worker replaces it with
    a ``completed`` row carrying the INSERT's row count. Ordered by (hunt_id, fire)
    rather than hunt_id alone, so a pending request cannot merge away the row count
    of the run before it. That keeps a row per fire rather than one per hunt, which
    is a few tens of bytes per fire and is also the run history the API reads.
    """
    return TableSpec(
        name="hunt_run",
        columns=[
            _ch_column("hunt_id", "String", order=0),
            _ch_column("fire", "Int64", order=1, comment="the scheduled fire this run covers"),
            _ch_column("status", "String", comment="requested (run now) or completed"),
            _ch_column("rows_written", "Int64", comment="rows the run's INSERT wrote"),
            _ch_column("updated", "DateTime64(3)", default="now64(3)"),
        ],
        config=DDLConfig(
            db=database,
            engine="ReplacingMergeTree(updated)",
            ttl_columns=[],
            projection_order_by=None,
            index_granularity=2048,
        ),
    )


def hunt_runner_heartbeat_spec(database: str) -> TableSpec:
    """One row per hunt runner: the last tick it started, and the poll it ticks on.

    A lease says a hunt is executing right now, so it is absent on an idle runner and
    cannot answer "is a runner alive". The beat is written at the top of every tick,
    whether or not anything is due. ``poll_seconds`` rides along so liveness is judged
    against the cadence the runner was actually started with rather than a setting the
    reader might resolve differently.
    """
    return TableSpec(
        name="hunt_runner_heartbeat",
        columns=[
            _ch_column("runner_id", "String", order=0),
            _ch_column("seen", "Int64", comment="epoch seconds of the tick, the runner's clock"),
            _ch_column("poll_seconds", "Float64", comment="seconds between this runner's ticks"),
            _ch_column("updated", "DateTime64(3)", default="now64(3)"),
        ],
        config=DDLConfig(
            db=database,
            engine="ReplacingMergeTree(updated)",
            ttl_columns=[],
            projection_order_by=None,
            index_granularity=2048,
        ),
    )


def hunt_coordination_specs(database: str) -> list[TableSpec]:
    """Every hunt coordination table, in the data database."""
    return [load_table_spec(ref, database) for ref in HUNT_COORDINATION_REFS] + [
        hunt_run_spec(database),
        hunt_runner_heartbeat_spec(database),
    ]


def internal_specs(database: str) -> list[TableSpec]:
    """Every engine-state table that lives in the internal database."""
    return [load_table_spec(ref, database) for ref in INTERNAL_REFS]
