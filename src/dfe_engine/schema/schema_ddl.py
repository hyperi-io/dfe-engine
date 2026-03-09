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
from datetime import datetime, timezone

from hyperi_pylib.logger import logger

from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry


class DDLGenerationError(Exception):
    """Error generating DDL."""


@dataclass
class DDLConfig:
    """Configuration for DDL generation.

    These settings come from the Source model (schema config, header config)
    and control table-level DDL properties.
    """

    db: str = "{db}"
    engine: str = "MergeTree"
    ttl_days: int | None = 90
    ttl_columns: list[str] = field(default_factory=lambda: ["_timestamp", "_timestamp_load"])
    partition_column: str = "_timestamp_load"
    index_granularity: int = 2048
    ttl_only_drop_parts: bool = True
    cluster: str | None = None
    sample_by: str | None = None
    projection_order_by: str | None = "_timestamp"
    schema_version: str = "2"
    profile: str | None = None
    profile_version: str | None = None


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
    followed by the human comment separated by `` — ``.
    """
    if expr and comment:
        return f"{expr} — {comment}"
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
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"-- HyperI DFE {table_name} {ts}")

        # CREATE TABLE
        create = f"CREATE TABLE IF NOT EXISTS {cfg.db}.{table_name}"
        if cfg.cluster:
            create += f" ON CLUSTER {cfg.cluster}"
        lines.append(f"{create}\n(")

        # Column + index + projection definitions
        body_lines = self._body_lines(columns, cfg)
        lines.append(",\n".join(body_lines))

        # Close columns, ENGINE
        lines.append(f")\nENGINE = {cfg.engine}()")

        # PARTITION BY
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
        ttl = self._ttl_clause(cfg)
        if ttl:
            lines.append(ttl)

        # COMMENT
        comment = self._table_comment(table_name, cfg)
        if comment:
            escaped = comment.replace("'", "\\'")
            lines.append(f"COMMENT '{escaped}'")

        # SETTINGS
        settings_parts = [f"index_granularity = {cfg.index_granularity}"]
        if cfg.ttl_only_drop_parts:
            settings_parts.append("ttl_only_drop_parts = 1")
        lines.append("SETTINGS\n    " + ",\n    ".join(settings_parts) + ";")

        return "\n".join(lines) + "\n"

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
            aliases.append(f"    `{column_name}` AS `{standard_field}`")

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
                    f"    PROJECTION timestamp_optimized "
                    f"(SELECT * ORDER BY `{cfg.projection_order_by}`)"
                )

        return body

    # ── Internal: column definition ─────────────────────────────────

    def _column_def(self, col: SchemaColumn) -> str:
        """Generate a single column definition line.

        Format:
            `name` Type [DEFAULT|MATERIALIZED|ALIAS expr] [CODEC(...)] [COMMENT '...']
        """
        ch_type, codec = self._resolve_type(col)
        parts = [f"`{col.name}`", ch_type]

        # DEFAULT / MATERIALIZED / ALIAS expression
        expr = self._default_expr(col)
        if expr:
            parts.append(expr)

        # CODEC
        if codec:
            parts.append(f"CODEC({codec})")

        # COMMENT — combines expr (DFE directive) + comment (human description)
        comment_text = _build_column_comment(col.expr, col.comment)
        if comment_text:
            escaped = comment_text.replace("'", "\\'")
            parts.append(f"COMMENT '{escaped}'")

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
                    f"Column '{col.name}': enum type without ch_override "
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

        Warns about nullable columns in ORDER BY.
        """
        ordered = [col for col in columns if col.order is not None]
        ordered.sort(key=lambda c: c.order)

        result: list[str] = []
        for col in ordered:
            # Check for nullable — warn but include
            ch_type, _ = self._resolve_type(col)
            if "Nullable" in ch_type:
                logger.warning(
                    f"Column '{col.name}' is Nullable but included in ORDER BY. "
                    f"Consider adding 'not_null' attribute for better performance."
                )
            result.append(f"`{col.name}`")

        return result

    # ── Internal: TTL ───────────────────────────────────────────────

    @staticmethod
    def _ttl_clause(cfg: DDLConfig) -> str | None:
        """Build the TTL clause."""
        if cfg.ttl_days is None or not cfg.ttl_columns:
            return None

        parts = []
        for col in cfg.ttl_columns:
            parts.append(f"{col} + INTERVAL {cfg.ttl_days} DAY DELETE WHERE {col} >= 0")

        return "TTL " + ",\n    ".join(parts)

    # ── Internal: table comment ─────────────────────────────────────

    @staticmethod
    def _table_comment(table_name: str, cfg: DDLConfig) -> str | None:
        """Build the table comment string with metadata tags."""
        tags = [f"@schema_version: {cfg.schema_version}"]

        if cfg.profile:
            tags.append(f"@profile: {cfg.profile}")
        if cfg.profile_version:
            tags.append(f"@profile_version: {cfg.profile_version}")

        return " | ".join(tags) if tags else None
