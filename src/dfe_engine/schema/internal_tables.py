#  Project:      dfe-engine
#  File:         schema/internal_tables.py
#  Purpose:      The engine's OWN ClickHouse tables, read from dfe-schemas
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The tables dfe-engine keeps for itself: hunt coordination, alert cooldown.

Defined in dfe-schemas under ``tables/internal/``; this module only names them.
They hold the engine's own working state rather than user data, so their
columns are exact ClickHouse types with no meta-schema mapping behind them.
"""

from __future__ import annotations

from dfe_engine.schema.schema_ddl import TableSpec
from dfe_engine.schema.table_loader import load_table_spec

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


def hunt_coordination_specs(database: str) -> list[TableSpec]:
    """Every hunt coordination table, in the data database."""
    return [load_table_spec(ref, database) for ref in HUNT_COORDINATION_REFS]


def internal_specs(database: str) -> list[TableSpec]:
    """Every engine-state table that lives in the internal database."""
    return [load_table_spec(ref, database) for ref in INTERNAL_REFS]
