"""DDL Generator v2 — ClickHouse DDL from SchemaColumn models + TypeRegistry.

Replaces the DataFrame-based ClickHouseSchema in schema_ch.py.
Uses TypeRegistry for primitive→CH type resolution and generates
complete CREATE TABLE statements.

Usage:
    from dfe_engine.schema.schema_ddl import DDLGenerator, DDLConfig
    from dfe_engine.source.type_registry import TypeRegistry

    registry = TypeRegistry.default()
    gen = DDLGenerator(registry)
    ddl = gen.generate_create_table("filebeat", columns, DDLConfig(ttl_days=90))
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scalo.logger import logger

from dfe_engine import __version__
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry


class DDLGenerationError(Exception):
    """Error generating DDL."""


def unsafe_ident_reason(name: str) -> str | None:
    """Why *name* is unsafe in a DDL identifier position, or None when safe.

    THE one charset rule for operator-suppliable identifiers, shared by every
    DDL sink (the view mappings here, ``fieldmap.remap_view``): empty, any of
    the control chars ``\\` ; ( ) ' " \\\\``, or whitespace is rejected - each
    would splice raw SQL past backtick quoting or an unquoted position. Each
    sink raises its own exception type from this reason.
    """
    if not name:
        return "empty identifier"
    if any(c in name for c in "`;()'\"\\") or any(c.isspace() for c in name):
        return (
            "backticks, quotes, parens, semicolons, backslashes and "
            "whitespace are not permitted in identifiers"
        )
    return None


def _safe_view_ident(name: str, *, what: str) -> str:
    """Validate a mapping identifier for the backtick-quoted view positions.

    A source's inline ``custom_mappings`` are settable by a source_write user,
    so a backtick (quote breakout), whitespace, or a DDL control char here
    would splice raw SQL into the CREATE VIEW executed against ClickHouse.
    Applies :func:`unsafe_ident_reason` (the rule shared with
    ``fieldmap.remap_view._safe_ident``) - defence in depth at the sink,
    whatever the caller validated.
    """
    if not name:
        raise DDLGenerationError(f"empty {what} in view mapping")
    reason = unsafe_ident_reason(name)
    if reason is not None:
        raise DDLGenerationError(f"unsafe {what} in view mapping: {name!r} ({reason})")
    return name


@dataclass
class DDLConfig:
    """Configuration for DDL generation.

    These settings come from the Source model (schema config, header config)
    and control table-level DDL properties.
    """

    db: str = "{db}"
    engine: str = "MergeTree"
    # ClickHouse topology: "single" -> <engine>() standalone (keeperless, e.g.
    # local dev); "replicated" -> argumentless Replicated<engine>, where the
    # server supplies the znode path/replica from its default_replica_path /
    # default_replica_name macros (infra-layer config, never in the DDL). The
    # replicated form is portable across on-prem Replicated databases and CH
    # Cloud (auto-substituted to SharedMergeTree).
    topology: str = "single"
    ttl_days: int | None = 90
    ttl_columns: list[str] = field(default_factory=lambda: ["_timestamp", "_timestamp_load"])
    partition_column: str = "_timestamp_load"
    index_granularity: int = 2048
    ttl_only_drop_parts: bool = True
    cluster: str | None = None
    sample_by: str | None = None
    projection_order_by: str | None = "_timestamp"
    schema_version: str | None = None
    profile_name: str | None = None
    profile_version: str | None = None
    description: str | None = None
    dfe_engine_version: str = __version__


# ── Index templates ─────────────────────────────────────────────────
# GA text index (v26.2+) — deterministic inverted index, row-level filtering
_INDEX_TEMPLATES: dict[str, str] = {
    "dimension": "INDEX {name} {col} TYPE set(0) GRANULARITY 4",
    "fulltext": "INDEX {name} {col} TYPE text(tokenizer=splitByNonAlpha) GRANULARITY 1",
    "text_search": "INDEX {name} {col} TYPE text(tokenizer=ngrams(3)) GRANULARITY 1",
    "range": "INDEX {name} {col} TYPE minmax GRANULARITY 4",
    "bloom": "INDEX {name} {col} TYPE bloom_filter GRANULARITY 4",
}

# Legacy fallback (pre-v25.10) — bloom-filter based indexes
_INDEX_TEMPLATES_LEGACY: dict[str, str] = {
    "dimension": "INDEX {name} {col} TYPE set(0) GRANULARITY 4",
    "fulltext": "INDEX {name} {col} TYPE tokenbf_v1(8192, 4, 0) GRANULARITY 4",
    "text_search": "INDEX {name} {col} TYPE ngrambf_v1(3, 256, 2, 0) GRANULARITY 4",
    "range": "INDEX {name} {col} TYPE minmax GRANULARITY 4",
    "bloom": "INDEX {name} {col} TYPE bloom_filter GRANULARITY 4",
}


def _build_column_comment(expr: str | None, comment: str | None) -> str | None:
    """Combine DFE directive and human comment into a single ClickHouse COMMENT.

    When both are present, the expr comes first (loader parses it),
    followed by the human comment separated by `` - ``.
    """
    if expr and comment:
        return f"{expr} - {comment}"
    return expr or comment or None


class DDLGenerator:
    """Generates ClickHouse DDL from SchemaColumn models.

    Uses TypeRegistry for primitive → ClickHouse type resolution.
    Produces CREATE TABLE, ALTER TABLE, and Sigma view DDL.
    """

    def __init__(
        self,
        registry: TypeRegistry,
        *,
        use_legacy_indexes: bool = False,
    ) -> None:
        """Initialize the DDL generator.

        Args:
            registry: TypeRegistry for type resolution.
            use_legacy_indexes: Use tokenbf/ngrambf instead of text index
                               (for ClickHouse < v25.10).
        """
        self._registry = registry
        self._index_templates = _INDEX_TEMPLATES_LEGACY if use_legacy_indexes else _INDEX_TEMPLATES

    # ── CREATE TABLE ────────────────────────────────────────────────

    def generate_create_table(
        self,
        table_name: str,
        columns: list[SchemaColumn],
        config: DDLConfig | None = None,
        generated_time: str | None = None,
    ) -> str:
        """Generate a complete CREATE TABLE IF NOT EXISTS statement.

        Args:
            table_name: ClickHouse table name.
            columns: Schema columns (profile header + source-specific).
            config: DDL configuration. Defaults to DDLConfig().

        Returns:
            Complete CREATE TABLE DDL string.
        """
        cfg = config or DDLConfig()
        lines: list[str] = []

        # Header comment
        lines.extend(
            [line for line in self._build_schema_header(cfg, table_name, generated_time) if line]
        )

        # CREATE TABLE
        create = f"CREATE TABLE IF NOT EXISTS {cfg.db}.{table_name}"
        if cfg.cluster:
            create += f" ON CLUSTER {cfg.cluster}"
        lines.append(f"{create}\n(")

        # Column + index + projection definitions
        body_lines = self._body_lines(columns, cfg)
        lines.append(",\n".join(body_lines))

        # Close columns, ENGINE (topology-aware).
        # "replicated" -> argumentless Replicated<engine>. We deliberately do NOT
        # emit the ('/znode/path','{replica}') args: the znode path + replica name
        # are the SERVER's job, supplied from its default_replica_path /
        # default_replica_name macros (an infra-layer concern, not the DDL). This
        # argumentless form is the only portable one - live-proven 2026-07-06:
        # accepted on-prem inside a Replicated database (or via ON CLUSTER) yielding
        # a real ReplicatedMergeTree, AND accepted on CH Cloud where it
        # auto-substitutes to SharedMergeTree. The explicit-path form is REJECTED
        # (code 36) by BOTH CH Cloud and on-prem Replicated databases, so we never
        # produce it. "single" -> plain <engine>() for a keeperless standalone
        # (local dev); on CH Cloud that too auto-substitutes to SharedMergeTree.
        # Render the ENGINE clause through the shared resolver: split cfg.engine
        # into variant + params and emit the topology-correct form. This is what
        # genericises beyond plain MergeTree - ANY family variant with its params
        # (ReplacingMergeTree(ver), SummingMergeTree(cols), ...) renders correctly:
        # single -> <variant>(params); replicated -> argumentless
        # Replicated<variant>(params) (no double-parens, no dropped ver). ON CLUSTER
        # stays on the header via cfg.cluster - this static config path never senses
        # a live cluster, so the resolver's sensed on_cluster is empty here.
        spec = parse_engine(cfg.engine)
        engine_clause = EngineResolver(override=cfg.topology).resolve(spec, cfg.db).clause
        lines.append(f")\nENGINE = {engine_clause}")

        # PARTITION BY
        if any(column for column in columns if column.name == cfg.partition_column):
            lines.append(f"PARTITION BY toYYYYMMDD({cfg.partition_column})")

        # ORDER BY + PRIMARY KEY
        order_cols = self._order_by_columns(columns)
        if order_cols:
            pk_str = ", ".join(order_cols)
            lines.append(f"PRIMARY KEY ({pk_str})")
            lines.append(f"ORDER BY ({pk_str})")
        else:
            lines.append("ORDER BY tuple()")

        # SAMPLE BY
        if cfg.sample_by:
            lines.append(f"SAMPLE BY {cfg.sample_by}")

        # TTL
        ttl = self._ttl_clause(cfg, columns)
        if ttl:
            lines.append(ttl)

        # SETTINGS
        settings_parts = [f"index_granularity = {cfg.index_granularity}"]
        if cfg.ttl_only_drop_parts:
            settings_parts.append("ttl_only_drop_parts = 1")
        lines.append("SETTINGS\n    " + ",\n    ".join(settings_parts))

        # COMMENT
        comment = self._table_comment(table_name, cfg)
        if comment:
            escaped = comment.replace("'", "\\'")
            lines.append(f"COMMENT '{escaped}'")

        return "\n".join(lines) + ";\n"

    # ── ALTER TABLE ─────────────────────────────────────────────────

    def generate_alter_add_column(
        self,
        table_name: str,
        column: SchemaColumn,
        config: DDLConfig | None = None,
        *,
        after: str | None = None,
    ) -> str:
        """Generate ALTER TABLE ADD COLUMN statement.

        Args:
            table_name: ClickHouse table name.
            column: Column to add.
            config: DDL configuration.
            after: Column name to place the new column after.

        Returns:
            ALTER TABLE DDL string.
        """
        cfg = config or DDLConfig()
        col_def = self._column_def(column)
        sql = f"ALTER TABLE {cfg.db}.{table_name} ADD COLUMN IF NOT EXISTS {col_def}"
        if after:
            sql += f" AFTER `{after}`"
        return sql + ";\n"

    def generate_alter_modify_column(
        self,
        table_name: str,
        column: SchemaColumn,
        config: DDLConfig | None = None,
    ) -> str:
        """Generate ALTER TABLE MODIFY COLUMN statement.

        Args:
            table_name: ClickHouse table name.
            column: Column with updated definition.
            config: DDL configuration.

        Returns:
            ALTER TABLE DDL string.
        """
        cfg = config or DDLConfig()
        col_def = self._column_def(column)
        return f"ALTER TABLE {cfg.db}.{table_name} MODIFY COLUMN {col_def};\n"

    def generate_alter_add_index(
        self,
        table_name: str,
        column: SchemaColumn,
        config: DDLConfig | None = None,
    ) -> str | None:
        """Generate ALTER TABLE ADD INDEX for a column's use_case, or None.

        Returns None when the column has no indexable use_case.
        """
        cfg = config or DDLConfig()
        idx = self._index_def(column)
        if not idx:
            return None
        return f"ALTER TABLE {cfg.db}.{table_name} ADD {idx};\n"

    # ── Standard Views ─────────────────────────────────────────────

    def generate_view(
        self,
        table_name: str,
        mappings: dict[str, str],
        suffix: str,
        config: DDLConfig | None = None,
    ) -> str:
        """Generate CREATE OR REPLACE VIEW with field aliases.

        View name: ``{table_name}_{suffix}``
        (e.g. ``windows_audit_sigma``, ``windows_audit_ecs``).

        Args:
            table_name: Base table name.
            mappings: standard_field → column_name mapping.
            suffix: View name suffix (e.g. "sigma", "ecs", "cim").
            config: DDL configuration.

        Returns:
            CREATE VIEW DDL string.
        """
        cfg = config or DDLConfig()
        view_name = f"{table_name}_{suffix}"

        aliases = []
        for standard_field, column_name in sorted(mappings.items()):
            safe_col = _safe_view_ident(column_name, what="column name")
            safe_field = _safe_view_ident(standard_field, what="standard field")
            aliases.append(f"    `{safe_col}` AS `{safe_field}`")

        if aliases:
            select_parts = ",\n".join(aliases) + ",\n    *"
        else:
            select_parts = "    *"

        return (
            f"CREATE OR REPLACE VIEW {cfg.db}.{view_name} AS\n"
            f"SELECT\n"
            f"{select_parts}\n"
            f"FROM {cfg.db}.{table_name};\n"
        )

    def generate_sigma_view(
        self,
        table_name: str,
        mappings: dict[str, str],
        config: DDLConfig | None = None,
    ) -> str:
        """Generate Sigma view (convenience wrapper for generate_view)."""
        return self.generate_view(table_name, mappings, "sigma", config)

    # ── Internal: body lines ────────────────────────────────────────

    def _build_schema_header(
        self, cfg: DDLConfig, table_name: str, generated_at: str | None = None
    ) -> list[str]:
        return [
            "-- =============================================================================",
            f"-- DFE Schema DDL: {table_name}",
            f"-- Profile: {cfg.profile_name}" if cfg.profile_name else "",
            f"-- {cfg.description}" if cfg.description else "",
            f"-- Generated by: dfe-engine {cfg.dfe_engine_version} DDLFileWriter",
            f"-- Generated at: {generated_at}" if generated_at else "",
            "--",
            "-- This file was auto-generated. Use {db} as database placeholder.",
            "-- =============================================================================",
        ]

    def _body_lines(
        self,
        columns: list[SchemaColumn],
        cfg: DDLConfig,
    ) -> list[str]:
        """Generate all lines inside the CREATE TABLE parentheses."""
        body: list[str] = []

        # Column definitions
        for col in columns:
            body.append(f"    {self._column_def(col)}")

        # Index definitions
        for col in columns:
            idx = self._index_def(col)
            if idx:
                body.append(f"    {idx}")

        # Projection
        if cfg.projection_order_by:
            col_names = {c.name for c in columns}
            if cfg.projection_order_by in col_names:
                body.append(
                    f"    PROJECTION {cfg.projection_order_by}_optimized "
                    f"(SELECT * ORDER BY `{cfg.projection_order_by}`)"
                )

        return body

    # ── Internal: column definition ─────────────────────────────────

    def _column_def(self, col: SchemaColumn) -> str:
        """Generate a single column definition line.

        Format:
            `name` Type [DEFAULT|MATERIALIZED|ALIAS expr] [COMMENT '...'] [CODEC(...)]
        """
        ch_type, codec = self._resolve_type(col)
        parts = [f"`{col.name}`", ch_type]

        # DEFAULT / MATERIALIZED / ALIAS expression
        expr = self._default_expr(col)
        if expr:
            parts.append(expr)

        # COMMENT — combines expr (DFE directive) + comment (human description)
        comment_text = _build_column_comment(col.expr, col.comment)
        if comment_text:
            escaped = comment_text.replace("'", "\\'")
            parts.append(f"COMMENT '{escaped}'")

        # CODEC
        if codec:
            parts.append(f"CODEC({codec})")

        return " ".join(parts)

    def _resolve_type(self, col: SchemaColumn) -> tuple[str, str | None]:
        """Resolve column to (ch_type, codec).

        Handles enum placeholder substitution by constructing
        a ch_override from the default field values.
        """
        ch_override = col.ch_override

        # Enum special case: construct Enum8(...) from default field values
        if col.type == "enum" and not ch_override:
            if col.default:
                ch_override = f"Enum8({col.default})"
            else:
                logger.warning(
                    f"Column {col.name!r}: enum type without ch_override "
                    f"or default values — using Enum8('') placeholder"
                )
                ch_override = "Enum8('')"

        # Note: use_case is NOT passed to resolve() — it doesn't affect type
        # resolution (only index generation). Use-case validation is handled
        # separately by SchemaLoader.validate_columns().
        resolved = self._registry.resolve(
            col.type,
            attributes=col.attribute,
            ch_override=ch_override,
        )

        codec = resolved.codec
        # When we constructed ch_override for enum, restore the primitive's codec
        if col.type == "enum" and ch_override and not col.ch_override:
            prim = self._registry.primitive_defaults(col.type)
            codec = prim.get("codec")

        # An explicit per-column codec always wins -- the only way to set a
        # codec alongside ch_override, which otherwise resolves to None.
        if col.codec:
            codec = col.codec

        return resolved.ch_type, codec

    @staticmethod
    def _default_expr(col: SchemaColumn) -> str | None:
        """Build the DEFAULT / MATERIALIZED / ALIAS clause."""
        if not col.default:
            return None

        if "materialized" in col.attribute:
            return f"MATERIALIZED {col.default}"
        if "alias" in col.attribute:
            return f"ALIAS {col.default}"
        return f"DEFAULT {col.default}"

    # ── Internal: index definition ──────────────────────────────────

    def _index_def(self, col: SchemaColumn) -> str | None:
        """Generate an INDEX definition for a column, or None."""
        if not col.use_case or col.use_case not in self._index_templates:
            return None

        template = self._index_templates[col.use_case]
        index_name = f"idx_{col.name}"
        return template.format(name=index_name, col=f"`{col.name}`")

    # ── Internal: ORDER BY ──────────────────────────────────────────

    def _order_by_columns(self, columns: list[SchemaColumn]) -> list[str]:
        """Extract ORDER BY column names sorted by order field.

        Design decision: Nullable columns are skipped from the sorting key
        (with a warning), never emitted as keys. ClickHouse refuses a
        Nullable column in a sorting key unless the merge-tree setting
        allow_nullable_key=1 is enabled, and even then a null in the sort key
        cripples index effectiveness and doubles storage. Rather than flip
        allow_nullable_key on -- or fail the whole table -- the engine drops
        the offending column from the key and warns. To keep a column in the
        ORDER BY, mark it 'not_null' (or give it a DEFAULT, which also forces
        non-null). See docs/data-plane/schema.md "Nullability Defaults".
        """
        ordered = [col for col in columns if col.order is not None]
        ordered.sort(key=lambda c: c.order)

        result: list[str] = []
        for col in ordered:
            # Skip any Nullable key, including LowCardinality(Nullable(T)).
            ch_type, _ = self._resolve_type(col)
            if "Nullable" in ch_type:
                logger.warning(
                    f"Column {col.name!r} resolves to {ch_type} and is marked "
                    f"as an ORDER BY key; skipping it from the sorting key. "
                    f"ClickHouse keys cannot be Nullable (requires "
                    f"allow_nullable_key and hurts index performance). Add the "
                    f"'not_null' attribute or a DEFAULT to keep it in the key."
                )
                continue
            result.append(f"`{col.name}`")

        return result

    # ── Internal: TTL ───────────────────────────────────────────────

    @staticmethod
    def _ttl_clause(cfg: DDLConfig, columns: list[SchemaColumn]) -> str | None:
        """Build the TTL clause."""
        if cfg.ttl_days is None or not cfg.ttl_columns:
            return None

        parts = []
        for col in cfg.ttl_columns:
            if any(column for column in columns if column.name == col):
                parts.append(f"{col} + INTERVAL {cfg.ttl_days} DAY DELETE WHERE {col} >= 0")

        if len(parts) == 0:
            return None
        return "TTL " + ",\n    ".join(parts)

    # ── Internal: table comment ─────────────────────────────────────

    @staticmethod
    def _table_comment(table_name: str, cfg: DDLConfig) -> str | None:
        """Build the table comment string with metadata tags."""
        tags = []

        if cfg.profile_name:
            tags.append(f"@profile: {cfg.profile_name}")
        if cfg.profile_version:
            tags.append(f"@profile_version: {cfg.profile_version}")
        if cfg.schema_version:
            tags.append(f"@{table_name}_version: {cfg.schema_version}")

        return " | ".join(tags) if tags else None
