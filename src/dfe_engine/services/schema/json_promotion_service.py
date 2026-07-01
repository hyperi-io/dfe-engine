#  Project:      dfe-engine
#  File:         src/dfe_engine/services/schema/json_promotion_service.py
#  Purpose:      Discover and promote JSON paths into dedicated schema columns
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""JSON field promotion service.

Discovers the dynamic paths inside a source's ``_json`` ClickHouse column and
promotes selected paths into dedicated, typed schema columns. Each promoted
column carries a ``@copy`` directive (see ``dfe_engine.source.expression``) so
dfe-loader copies the value forward from ``_json`` on subsequent ingest.

Two responsibilities, kept separate so the column maths is pure and testable:

* ``discover_paths`` -- runs the ClickHouse discovery / sample / stats queries.
* ``build_promotion_columns`` -- derives ``SchemaColumn`` definitions and
  per-path outcomes with no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from dfe_engine.schema.models import SchemaColumn as MetaSchemaColumn
from dfe_engine.source.expression import ExpressionBuilder, ExpressionValidator
from dfe_engine.source.models import SchemaColumn

# Standard JSON column name across all schema profiles (timeseries, minimal,
# passthrough). Single source of truth -- import this rather than hard-coding.
JSON_COLUMN = "_json"

# Requested CH index family -> schema use_case (drives DDL index generation via
# DDLGenerator._index_def). The use_case is validated against the column's
# primitive by TypeRegistry, so an illegal pairing surfaces as a per-path error.
INDEX_TYPE_TO_USE_CASE: dict[str, str] = {
    "set": "dimension",
    "minmax": "range",
    "bloom_filter": "bloom",
    "tokenbf_v1": "fulltext",
    "ngrambf_v1": "text_search",
}

_WRAPPER_RE = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)$")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_IDENT_RE = re.compile(r"[^0-9a-zA-Z]+")


class JsonPromotionError(Exception):
    """Raised when path discovery or promotion fails at the service layer."""


@dataclass
class DiscoveredPath:
    """One JSON path discovered inside the ``_json`` column."""

    path: str
    types: list[str]
    is_consistent: bool
    suggested_column_name: str
    promoted_to: str | None = None
    coverage_pct: float | None = None
    distinct_count: int | None = None
    samples: list[str] | None = None
    # Draft column the path would become in a meta-schema (server-derived so the
    # editor does not re-map ClickHouse types). Derived from the first observed
    # type; check ``is_consistent`` before trusting it for a multi-type path.
    column_type: str | None = None
    column_attributes: list[str] = field(default_factory=list)
    copy_expr: str = ""


@dataclass
class PromotionRequest:
    """A single requested promotion (one JSON path -> one column)."""

    json_path: str
    column_name: str | None = None
    data_type: str | None = None
    index_type: str | None = None


@dataclass
class PromotionOutcome:
    """Per-path result of building a promotion column."""

    json_path: str
    status: str  # "ok" | "error"
    column_name: str | None = None
    data_type: str | None = None
    index_type: str | None = None
    copy_cel: str | None = None
    error: str | None = None
    column: SchemaColumn | None = field(default=None)


# ── Naming / type helpers (pure) ─────────────────────────────────────


def qualified_table(db: str, source: str) -> str:
    """Backtick-quoted ``db.table`` identifier for the discovery queries."""
    return f"`{db}`.`{source}`"


def clickhouse_table_exists(client: Any, db: str, table: str) -> bool:
    """Return whether ``db.table`` is present in ClickHouse."""
    try:
        rows = client.execute(
            "SELECT 1 FROM system.tables "
            "WHERE database = {db:String} AND name = {tbl:String} LIMIT 1",
            parameters={"db": db, "tbl": table},
        )
        return bool(rows)
    except Exception:
        return False


def copy_cel_for_path(path: str) -> str:
    """CEL path expression that reads ``path`` from the JSON column."""
    return f"{JSON_COLUMN}.{path}"


def ch_dynamic_type_to_primitive(ch_type: str) -> tuple[str, list[str]]:
    """Map a ClickHouse dynamic type (from JSONDynamicPathsWithTypes) to a
    DFE primitive + storage attributes.

    Unwraps ``Nullable(...)`` / ``LowCardinality(...)`` first. Unknown or
    compound types (Array/Tuple/Map/Object/JSON) fall back to ``json``.
    """
    base = ch_type.strip()
    match = _WRAPPER_RE.match(base)
    while match:
        base = match.group(1).strip()
        match = _WRAPPER_RE.match(base)

    if base.startswith(("Int", "UInt")):
        return "integer", []
    if base.startswith("Float"):
        return "float", []
    if base in ("Bool", "Boolean"):
        return "boolean", []
    if base.startswith("DateTime"):
        return "datetime", []
    if base.startswith("Date"):
        return "date", []
    if base == "UUID":
        return "uuid", []
    if base.startswith("IPv"):
        return "ip", []
    if base == "String" or base.startswith("FixedString"):
        return "string", []
    return "json", ["nullable"]


def suggested_column_name(path: str, existing: set[str]) -> str:
    """Derive a snake_case column name from a dotted JSON path.

    Splits camelCase, replaces dots / non-identifier chars with underscores,
    lowercases, guards a leading digit, then suffixes ``_2``, ``_3``, ... until
    it no longer collides with ``existing``.
    """
    snake = _CAMEL_BOUNDARY_RE.sub("_", path)
    snake = _NON_IDENT_RE.sub("_", snake).strip("_").lower()
    snake = re.sub(r"_+", "_", snake)
    if not snake:
        snake = "field"
    if snake[0].isdigit():
        snake = f"f_{snake}"

    candidate = snake
    suffix = 2
    while candidate in existing:
        candidate = f"{snake}_{suffix}"
        suffix += 1
    return candidate


def promoted_paths(columns: list[MetaSchemaColumn]) -> dict[str, str]:
    """Map already-promoted JSON paths to their column name.

    Parses each column's ``expr`` for a ``@copy: _json.<path>`` directive.
    """
    out: dict[str, str] = {}
    prefix = f"{JSON_COLUMN}."
    for col in columns:
        expr = getattr(col, "expr", None)
        if not expr:
            continue
        result = ExpressionValidator.validate(expr)
        if result.valid and result.directive == "copy" and result.copy_path:
            json_path = result.copy_path
            if json_path.startswith(prefix):
                json_path = json_path[len(prefix) :]
            out[json_path] = col.name
    return out


def _json_subcolumn(path: str) -> str:
    """SQL accessor for a discovered JSON path, e.g. ``assumeNotNull(_json).`user.email```.

    The whole dotted path is one backtick-quoted identifier (that is how
    JSONDynamicPathsWithTypes reports nested paths). ``assumeNotNull`` unwraps the
    ``Nullable(JSON)`` column (a no-op on a non-nullable column) so subcolumn
    access type-checks. Rejects backticks to keep the identifier injection-safe.
    """
    if "`" in path:
        raise JsonPromotionError(f"Illegal JSON path: {path!r}")
    return f"assumeNotNull({JSON_COLUMN}).`{path}`"


def _match_accessor(match_field: str) -> str:
    """SQL accessor for a source match field.

    A field prefixed with ``_json.`` names a path *inside* the JSON column and
    resolves to a subcolumn (the ``_json.`` is the column, not part of the
    path). Any other field names a real top-level table column and is referenced
    directly, so a match can target a column that lives outside the JSON object
    (e.g. ``_org_id``). Rejects backticks either way to keep it injection-safe.
    """
    prefix = f"{JSON_COLUMN}."
    if match_field.startswith(prefix):
        return _json_subcolumn(match_field[len(prefix) :])
    if "`" in match_field:
        raise JsonPromotionError(f"Illegal match field: {match_field!r}")
    return f"`{match_field}`"


def _match_condition(
    match_field: str | None, match_value: str | None
) -> tuple[str, dict[str, Any]]:
    """SQL boolean condition + params restricting rows to one source's match rule.

    Returns ``("", {})`` when no match is supplied (the source owns its whole
    table). Otherwise compares ``match_field`` to ``match_value`` as a string --
    used when discovering against the shared catch-all landing table, where a
    source's rows are identified by its match. ``match_field`` is resolved by
    ``_match_accessor``: ``_json.<path>`` targets a JSON subcolumn, a bare name
    targets a real column. The value is parameterised (injection-safe); the
    field goes through ``_match_accessor`` which rejects backticks.
    """
    if not (match_field and match_value):
        return "", {}
    sub = _match_accessor(match_field)
    return f"toString({sub}) = {{match_value:String}}", {"match_value": match_value}


# ── Discovery (I/O) ──────────────────────────────────────────────────


def discover_paths(
    client: Any,
    *,
    db: str,
    source: str,
    existing_columns: list[MetaSchemaColumn],
    match_field: str | None = None,
    match_value: str | None = None,
    paths: list[str] | None = None,
    samples: int | None = None,
    stats: bool = False,
) -> list[DiscoveredPath]:
    """Discover JSON paths in ``db.source._json`` with optional samples/stats.

    When ``match_field``/``match_value`` are supplied, rows are restricted to a
    single source's match rule -- used to discover against the shared catch-all
    landing table before the source has its own table. Without them, the whole
    table is scanned.

    Raises:
        JsonPromotionError: when the underlying ClickHouse query fails (e.g. the
            source table does not exist yet).
    """
    table = qualified_table(db, source)
    promoted = promoted_paths(existing_columns)
    existing_names = {col.name for col in existing_columns}
    match_sql, match_params = _match_condition(match_field, match_value)

    sql = (
        f"SELECT tup.1 AS path, tup.2 AS type FROM {table} "
        f"ARRAY JOIN JSONDynamicPathsWithTypes(assumeNotNull({JSON_COLUMN})) AS tup "
    )
    if match_sql:
        sql += f"WHERE {match_sql} "
    sql += "GROUP BY path, type"
    params: dict[str, Any] = dict(match_params)
    if paths:
        sql += " HAVING path IN {paths:Array(String)}"
        params["paths"] = paths
    sql += " ORDER BY path"

    try:
        rows = client.execute(sql, parameters=params)
    except JsonPromotionError:
        raise
    except Exception as exc:
        raise JsonPromotionError(f"JSON path discovery failed: {exc}") from exc

    # Collapse (path, type) rows into one record per path, preserving first-seen
    # type order for stable output.
    types_by_path: dict[str, list[str]] = {}
    for path, ch_type in rows:
        bucket = types_by_path.setdefault(str(path), [])
        if str(ch_type) not in bucket:
            bucket.append(str(ch_type))

    discovered: list[DiscoveredPath] = []
    for path, types in types_by_path.items():
        suggested = suggested_column_name(path, existing_names)
        primitive, attributes = ch_dynamic_type_to_primitive(types[0]) if types else (None, [])
        item = DiscoveredPath(
            path=path,
            types=types,
            is_consistent=len(types) == 1,
            suggested_column_name=suggested,
            promoted_to=promoted.get(path),
            column_type=primitive,
            column_attributes=attributes,
            copy_expr=ExpressionBuilder.copy(copy_cel_for_path(path)),
        )
        if samples:
            item.samples = _fetch_samples(client, table, path, samples, match_sql, match_params)
        if stats:
            item.coverage_pct, item.distinct_count = _fetch_stats(
                client, table, path, match_sql, match_params
            )
        discovered.append(item)

    return discovered


def _fetch_samples(
    client: Any,
    table: str,
    path: str,
    n: int,
    match_sql: str = "",
    match_params: dict[str, Any] | None = None,
) -> list[str]:
    """Random, distinct example values for a path (ORDER BY rand())."""
    sub = _json_subcolumn(path)
    where = f"{sub} IS NOT NULL"
    if match_sql:
        where += f" AND {match_sql}"
    sql = (
        f"SELECT DISTINCT toString({sub}) AS value FROM {table} "
        f"WHERE {where} ORDER BY rand() LIMIT {{n:UInt32}}"
    )
    try:
        rows = client.execute(sql, parameters={"n": n, **(match_params or {})})
    except Exception as exc:
        raise JsonPromotionError(f"Sampling failed for path {path!r}: {exc}") from exc
    return [str(row[0]) for row in rows]


def _fetch_stats(
    client: Any,
    table: str,
    path: str,
    match_sql: str = "",
    match_params: dict[str, Any] | None = None,
) -> tuple[float | None, int | None]:
    """Coverage percentage and approximate distinct count for a path."""
    sub = _json_subcolumn(path)
    sql = (
        "SELECT "
        f"100.0 * countIf({sub} IS NOT NULL) / count() AS coverage_pct, "
        f"uniqHLL12({sub}) AS distinct_count "
        f"FROM {table}"
    )
    if match_sql:
        sql += f" WHERE {match_sql}"
    try:
        rows = client.execute(sql, parameters=dict(match_params or {}))
    except Exception as exc:
        raise JsonPromotionError(f"Stats failed for path {path!r}: {exc}") from exc
    if not rows:
        return None, None
    coverage, distinct = rows[0]
    return (
        float(coverage) if coverage is not None else None,
        int(distinct) if distinct is not None else None,
    )


# ── Row sampling (I/O) ───────────────────────────────────────────────


def sample_rows(
    client: Any,
    *,
    db: str,
    source: str,
    match_field: str | None = None,
    match_value: str | None = None,
    limit: int = 10,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Random sample rows from ``db.source``, scoped to a source's match rule.

    When ``match_field``/``match_value`` are supplied, rows are restricted to a
    single source's match rule -- used to sample the shared catch-all landing
    table where a source's rows are identified by its match. Without them, the
    whole table is sampled. Intended for inspecting real data while authoring a
    match condition or CEL before any path is promoted.

    Returns ``(column_names, rows)`` where each row is a ``column -> value``
    mapping. ``column_names`` is returned even when no rows match, so callers
    still learn the table shape.

    Raises:
        JsonPromotionError: when the underlying ClickHouse query fails (e.g. the
            source table does not exist yet).
    """
    table = qualified_table(db, source)
    match_sql, match_params = _match_condition(match_field, match_value)
    sql = f"SELECT * FROM {table} "
    if match_sql:
        sql += f"WHERE {match_sql} "
    sql += "ORDER BY rand() LIMIT {limit:UInt32}"
    params: dict[str, Any] = {"limit": limit, **match_params}

    try:
        columns, rows = client.query_rows(sql, parameters=params)
    except Exception as exc:
        raise JsonPromotionError(f"Row sampling failed: {exc}") from exc

    records = [dict(zip(columns, row, strict=True)) for row in rows]
    return list(columns), records


# ── Column building (pure) ───────────────────────────────────────────


def build_promotion_columns(
    existing_columns: list[MetaSchemaColumn],
    requests: list[PromotionRequest],
    *,
    type_registry: Any,
    path_types: dict[str, list[str]],
) -> list[PromotionOutcome]:
    """Derive promotion columns + per-path outcomes (no I/O).

    Args:
        existing_columns: current meta-schema version columns.
        requests: requested promotions.
        type_registry: ``TypeRegistry`` for validation.
        path_types: discovered ClickHouse types per JSON path (for auto-derive).

    Returns:
        One ``PromotionOutcome`` per request, in request order. ``ok`` outcomes
        carry the built ``SchemaColumn`` on ``.column``.
    """
    already_promoted = promoted_paths(existing_columns)
    reserved = {col.name for col in existing_columns}
    outcomes: list[PromotionOutcome] = []

    for req in requests:
        path = req.json_path

        if path in already_promoted:
            outcomes.append(
                PromotionOutcome(
                    json_path=path,
                    status="error",
                    error=f"path already promoted to column '{already_promoted[path]}'",
                )
            )
            continue

        # Resolve the primitive type.
        if req.data_type:
            primitive, attributes = req.data_type, []
        else:
            types = path_types.get(path)
            if not types:
                outcomes.append(
                    PromotionOutcome(
                        json_path=path,
                        status="error",
                        error=f"path {path!r} not found in {JSON_COLUMN}",
                    )
                )
                continue
            if len(types) > 1:
                outcomes.append(
                    PromotionOutcome(
                        json_path=path,
                        status="error",
                        error=(
                            f"type conflict: {' | '.join(types)} -- declare data_type explicitly"
                        ),
                    )
                )
                continue
            primitive, attributes = ch_dynamic_type_to_primitive(types[0])

        # Resolve the index use_case (optional).
        use_case: str | None = None
        if req.index_type:
            use_case = INDEX_TYPE_TO_USE_CASE.get(req.index_type)
            if use_case is None:
                outcomes.append(
                    PromotionOutcome(
                        json_path=path,
                        status="error",
                        error=(
                            f"unknown index_type '{req.index_type}'. Valid: "
                            f"{', '.join(sorted(INDEX_TYPE_TO_USE_CASE))}"
                        ),
                    )
                )
                continue

        # Resolve the column name (explicit names must not collide; suggested
        # names auto-suffix).
        if req.column_name:
            column_name = req.column_name
            if column_name in reserved:
                outcomes.append(
                    PromotionOutcome(
                        json_path=path,
                        status="error",
                        error=f"column name '{column_name}' already exists",
                    )
                )
                continue
        else:
            column_name = suggested_column_name(path, reserved)

        copy_cel = copy_cel_for_path(path)
        column = SchemaColumn(
            name=column_name,
            type=primitive,
            attribute=attributes,
            use_case=use_case,
            expr=ExpressionBuilder.copy(copy_cel),
            comment=f"Promoted from {JSON_COLUMN}.{path}",
        )

        errors = column.validate_against_registry(type_registry)
        if errors:
            outcomes.append(
                PromotionOutcome(
                    json_path=path,
                    status="error",
                    error="; ".join(errors),
                )
            )
            continue

        reserved.add(column_name)
        outcomes.append(
            PromotionOutcome(
                json_path=path,
                status="ok",
                column_name=column_name,
                data_type=primitive,
                index_type=req.index_type,
                copy_cel=copy_cel,
                column=column,
            )
        )

    return outcomes


def promotion_preview_ddl(
    table_name: str,
    columns: list[SchemaColumn],
    *,
    db: str,
    use_legacy_indexes: bool = False,
) -> list[str]:
    """ALTER TABLE statements that would realise the promoted columns.

    One ADD COLUMN per column (carrying the ``@copy`` directive in its COMMENT),
    plus an ADD INDEX for any column with an index use_case.
    """
    from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
    from dfe_engine.source.type_registry import TypeRegistry

    generator = DDLGenerator(TypeRegistry.default(), use_legacy_indexes=use_legacy_indexes)
    config = DDLConfig(db=db)
    statements: list[str] = []
    for column in columns:
        statements.append(generator.generate_alter_add_column(table_name, column, config).strip())
        index_stmt = generator.generate_alter_add_index(table_name, column, config)
        if index_stmt:
            statements.append(index_stmt.strip())
    return statements
