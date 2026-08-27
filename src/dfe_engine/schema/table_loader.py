#  Project:      dfe-engine
#  File:         schema/table_loader.py
#  Purpose:      Build a TableSpec from a dfe-schemas `tables/` YAML definition
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Read a table written in exact ClickHouse types from dfe-schemas.

The `meta/` schemas map DFE's primitives onto ClickHouse; the `tables/` ones do
not map anything, because their columns are engine state and telemetry rather
than user data. Format reference: ``docs/tables.md`` in dfe-schemas.

The definitions live only in dfe-schemas, so a missing tree raises rather than
falling back -- a silently empty table list would let the data plane start
against a database with nothing in it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.schema.schema_ddl import DDLConfig, TableSpec
from dfe_engine.schema.schema_loader import (
    SchemaLoader,
    SchemaLoadError,
    _resolve_schemas_root,
)
from dfe_engine.source.models import SchemaColumn

INHERIT = "inherit"
"""``max_dynamic_paths: inherit`` -- take the value from the common header."""

_HEADER_JSON_COLUMN = "_json"
_HEADER_PROFILE = "timeseries"
_FALLBACK_JSON_MAX_PATHS = 2048


def _table_yaml_path(ref: str) -> Path:
    """Locate one ``tables/...`` definition inside the dfe-schemas tree."""
    root = _resolve_schemas_root()
    if root is None:
        raise SchemaLoadError(
            f"Cannot resolve the dfe-schemas tree, so {ref!r} is unreadable. "
            "Set DFE_SCHEMAS_DIR, check out the schemas submodule, or run from "
            "an image that seeds them."
        )
    path = root / f"{ref}.yaml"
    if not path.exists():
        raise SchemaLoadError(f"Table definition not found: {path}")
    return path


def _header_json_max_paths() -> int:
    """Typed sub-paths a JSON column holds before the rest spill to a map.

    Read from the common header's ``_json`` rather than restated, so the JSON
    columns in the stack cannot drift apart.
    """
    try:
        for column in SchemaLoader.load_profile(profile_name=_HEADER_PROFILE):
            if column.name == _HEADER_JSON_COLUMN and column.max_dynamic_paths:
                return int(column.max_dynamic_paths)
    except Exception as exc:
        logger.warning(f"common header unreadable, using the JSON path fallback: {exc}")
    return _FALLBACK_JSON_MAX_PATHS


def _column(raw: dict[str, Any], source: Path) -> SchemaColumn:
    """One column, typed by exact ClickHouse type.

    ``ch_override`` rather than a DFE primitive: there is no primitive mapping
    to route these through, and the override path resolves non-nullable, which
    is what a sorting key needs.
    """
    name = raw.get("name")
    ch_type = raw.get("ch_type")
    if not name or not ch_type:
        raise SchemaLoadError(f"Column needs both 'name' and 'ch_type' in {source}: {raw!r}")

    attribute: list[str] = []
    if raw.get("lowcardinality"):
        attribute.append("lowcardinality")

    default = raw.get("default")
    if "materialized" in raw:
        if default is not None:
            raise SchemaLoadError(
                f"Column {name!r} in {source} sets both 'default' and 'materialized'"
            )
        attribute.append("materialized")
        default = raw["materialized"]

    max_paths = raw.get("max_dynamic_paths")
    if max_paths == INHERIT:
        max_paths = _header_json_max_paths()

    return SchemaColumn(
        name=name,
        type="string",
        ch_override=ch_type,
        attribute=attribute,
        order=raw.get("order"),
        default=default,
        codec=raw.get("codec"),
        comment=raw.get("comment"),
        max_dynamic_paths=max_paths,
    )


def _config(table: dict[str, Any], database: str) -> DDLConfig:
    """DDL config for one table.

    Every clause is taken as written: these order by expressions rather than
    bare columns and index over map projections, neither of which the column
    model expresses. TTL and projection default to OFF rather than to the
    data-table defaults, which would add clauses over columns these do not
    have.
    """
    defaults = DDLConfig()
    return DDLConfig(
        db=database,
        engine=table.get("engine", "MergeTree"),
        ttl_days=table.get("ttl_days"),
        ttl_columns=list(table.get("ttl_columns") or []),
        projection_order_by=table.get("projection_order_by"),
        partition_by=table.get("partition_by"),
        partition_column=table.get("partition_column", defaults.partition_column),
        partition_granularity=table.get("partition_granularity", defaults.partition_granularity),
        order_by=table.get("order_by"),
        extra_indexes=list(table.get("indexes") or []),
        index_granularity=int(table.get("index_granularity", 2048)),
    )


def load_table_config(ref: str, database: str, *, version: str | None = None) -> DDLConfig:
    """The DDL config a ``tables/...`` definition declares, without its columns.

    The core tables compose their columns from a header profile and a hunts
    schema, so their definitions carry a ``table`` block and nothing else.

    Args:
        ref: Path under the schemas root without a suffix, e.g. ``tables/core/default``.
        database: The database the table lands in.
        version: Version to load. Defaults to the file's ``current`` marker.
    """
    path = _table_yaml_path(ref)
    entry = SchemaLoader.load_version_entry(path, version=version, require_columns=False)
    table = entry.get("table") or {}
    if "name" not in table:
        raise SchemaLoadError(f"Table definition {path} does not name its table")
    return _config(table, database)


def load_table_spec(ref: str, database: str, *, version: str | None = None) -> TableSpec:
    """Build the TableSpec for one ``tables/...`` definition.

    Args:
        ref: Path under the schemas root without a suffix, e.g. ``tables/otel/logs``.
        database: The database the table lands in.
        version: Version to load. Defaults to the file's ``current`` marker.
    """
    path = _table_yaml_path(ref)
    entry = SchemaLoader.load_version_entry(path, version=version)
    table = entry.get("table") or {}
    if "name" not in table:
        raise SchemaLoadError(f"Table definition {path} does not name its table")
    return TableSpec(
        name=table["name"],
        columns=[_column(raw, path) for raw in entry["columns"]],
        config=_config(table, database),
    )


def load_view_ddl(ref: str, database: str, on_cluster: str = "") -> tuple[str, str] | None:
    """The materialised view a definition declares, as ``(name, DDL)``.

    Returns None when the file declares none. A view has no columns to
    reconcile, only a SELECT, so it is not a TableSpec.
    """
    path = _table_yaml_path(ref)
    view = SchemaLoader.load_version_entry(path).get("materialized_view")
    if not view:
        return None
    select = view["select"].format(db=database)
    return (
        view["name"],
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {database}.{view['name']}{on_cluster} "
        f"TO {database}.{view['to']} AS\n{select}",
    )
