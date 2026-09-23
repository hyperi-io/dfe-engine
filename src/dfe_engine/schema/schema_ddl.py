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

from dataclasses import dataclass, field, replace

from scalo.logger import logger

from dfe_engine.schema.engine_resolver import EngineResolver, ResolvedEngine, parse_engine
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import (
    InvalidUseCaseError,
    TypeRegistry,
    split_use_case,
)


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


def quote_ident(name: str, *, what: str = "identifier") -> str:
    """Backtick-quote *name* for a DDL identifier position, after checking it is safe.

    Every identifier is quoted, not only the ones that would otherwise break --
    one shape to read, with no branch to get wrong. A source name may carry a
    hyphen, and unquoted ``db.my-source`` parses as a subtraction rather than a
    table, so without the quoting the statement does not parse.

    Quoting is never the only defence. :func:`unsafe_ident_reason` runs first and
    refuses a backtick, so a name can never close the quoting it is wrapped in.
    """
    reason = unsafe_ident_reason(name)
    if reason is not None:
        raise DDLGenerationError(f"unsafe {what}: {name!r} ({reason})")
    return f"`{name}`"


def _qualified(db: str, name: str, *, what: str) -> str:
    """A ``db.object`` reference with both halves quoted."""
    return f"{quote_ident(db, what='database')}.{quote_ident(name, what=what)}"


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
    # Cloud (auto-substituted to SharedMergeTree). Only consulted when the
    # generator has no injected resolver - it carries no ON CLUSTER intent, so a
    # multi-node cluster needs the sensing resolver (see DDLGenerator.resolver).
    topology: str = "single"
    # Retention is DECLARED, in dfe-schemas or a source's config, never defaulted
    # here: a default no operator can see is how a table ends up keeping
    # everything or dropping what it should not. None is undeclared; 0 declares no TTL.
    ttl_days: int | None = None
    # TTL rides the partition column alone: ttl_only_drop_parts drops a part only
    # once every row in it has expired, so a second rule over event time can only
    # ever delay the drop.
    ttl_columns: list[str] = field(default_factory=lambda: ["_timestamp_load"])
    partition_column: str = "_timestamp_load"
    # Partition granularity for *partition_column*: "day" -> toYYYYMMDD,
    # "month" -> toYYYYMM. A low-volume audit table wants monthly, else it
    # accumulates a part per day holding very few rows.
    partition_granularity: str = "day"
    # Raw clause overrides, for tables whose shape the column model cannot
    # express: an ORDER BY over expressions rather than bare columns
    # (toStartOfHour(TimeUnix), cityHash64(Attributes)), a PARTITION BY the
    # granularity enum does not cover (toDate(Timestamp)), or an index over a
    # map projection (mapKeys(ResourceAttributes) TYPE bloom_filter(0.01)).
    # Each is emitted verbatim, so a caller supplying one owns its correctness.
    partition_by: str | None = None
    order_by: str | None = None
    primary_key: str | None = None
    extra_indexes: list[str] = field(default_factory=list)
    index_granularity: int = 2048
    ttl_only_drop_parts: bool = True
    cluster: str | None = None
    sample_by: str | None = None
    projection_order_by: str | None = "_timestamp"
    schema_version: str | None = None
    profile_name: str | None = None
    profile_version: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class TableSpec:
    """One table, fully resolved: its name, its columns, and its DDL config.

    The unit the live apply path works in. A rendered CREATE TABLE string is
    enough to create a table that is absent, but not to reconcile one that
    exists -- that needs the column list to diff against ``system.columns``.
    Everything that can create a DFE table hands back one of these so both
    paths read from the same description.
    """

    name: str
    columns: list[SchemaColumn]
    config: DDLConfig


def with_default_ttl(spec: TableSpec, days: int | None) -> TableSpec:
    """*spec* with the deployment default retention, unless it declares its own.

    A declared ``ttl_days`` always wins; ``days`` of None leaves the spec as it
    is, and 0 declares no TTL. The config is copied, never mutated in place.
    """
    # A table with no ttl_columns cannot carry a TTL, and a default over none fails the CREATE.
    if (days is None) or (spec.config.ttl_days is not None) or not (spec.config.ttl_columns):
        return spec
    return replace(spec, config=replace(spec.config, ttl_days=days))


# ── Index templates ─────────────────────────────────────────────────
# A use case names the question a column is asked; the template is the engine's
# answer, and changes without the vocabulary changing.
# GA text index (v26.2+) — deterministic inverted index, row-level filtering
# ClickHouse rewrites a text index's GRANULARITY to 100000000 whatever is asked
# for, so a read-back that disagrees with the 1 below is the server, not drift.
_INDEX_TEMPLATES: dict[str, str] = {
    "dimension": "INDEX {name} {col} TYPE set(0) GRANULARITY 4",
    "range": "INDEX {name} {col} TYPE minmax GRANULARITY 4",
    "word_search": "INDEX {name} {col} TYPE text(tokenizer=splitByNonAlpha) GRANULARITY 1",
    "substring_search": "INDEX {name} {col} TYPE text(tokenizer=ngrams(3)) GRANULARITY 1",
}

# Legacy fallback (pre-v25.10) — bloom-filter based indexes
_INDEX_TEMPLATES_LEGACY: dict[str, str] = {
    "dimension": "INDEX {name} {col} TYPE set(0) GRANULARITY 4",
    "range": "INDEX {name} {col} TYPE minmax GRANULARITY 4",
    "word_search": "INDEX {name} {col} TYPE tokenbf_v1(8192, 4, 0) GRANULARITY 4",
    "substring_search": "INDEX {name} {col} TYPE ngrambf_v1(3, 256, 2, 0) GRANULARITY 4",
}

# exact_match picks on declared cardinality: set(0) holds every distinct value
# of a LowCardinality column exactly, bloom_filter stays bounded on the rest.
# Neither changed at v25.10, so the legacy fallback shares them.
_EXACT_MATCH_LOW_CARDINALITY = "INDEX {name} {col} TYPE set(0) GRANULARITY 4"
_EXACT_MATCH_HIGH_CARDINALITY = "INDEX {name} {col} TYPE bloom_filter GRANULARITY 4"

# A text index refuses a Map column outright ("Text index must be created on
# columns of type with base type of String or FixedString"), so key_search
# indexes the keys and the values apart -- the shape the shipped otel tables use.
_KEY_SEARCH_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("key", "INDEX {name} mapKeys({col}) TYPE text(tokenizer=array) GRANULARITY 1"),
    ("value", "INDEX {name} mapValues({col}) TYPE text(tokenizer=array) GRANULARITY 1"),
)

# The array tokenizer arrived with the GA text index, so a server old enough to
# need the legacy fallback cannot answer key_search at all.
_KEY_SEARCH_TEMPLATES_LEGACY: tuple[tuple[str, str], ...] = (
    ("key", "INDEX {name} mapKeys({col}) TYPE bloom_filter(0.01) GRANULARITY 1"),
    ("value", "INDEX {name} mapValues({col}) TYPE bloom_filter(0.01) GRANULARITY 1"),
)

# hnsw is the only method ClickHouse 26.3 implements and the index will not
# build without a dimension count, which is the one thing only the user knows.
_SIMILARITY_SEARCH_TEMPLATE = (
    "INDEX {name} {col} TYPE vector_similarity('hnsw', 'cosineDistance', {dims}) GRANULARITY 1"
)


def _with_max_dynamic_paths(ch_type: str, max_dynamic_paths: int | None) -> str:
    """Apply a column's ``max_dynamic_paths`` to a bare ``JSON`` type.

    A type that already carries parameters is left alone, and the setting is
    meaningless on anything but JSON, so both cases pass through unchanged
    rather than producing DDL ClickHouse will reject.
    """
    if not max_dynamic_paths or ch_type != "JSON":
        return ch_type
    return f"JSON(max_dynamic_paths={max_dynamic_paths})"


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
        resolver: EngineResolver | None = None,
    ) -> None:
        """Initialize the DDL generator.

        Args:
            registry: TypeRegistry for type resolution.
            use_legacy_indexes: Use tokenbf/ngrambf instead of text index
                               (for ClickHouse < v25.10).
            resolver: Engine resolver to select the table engine. Pass one built
                     with a live client to enable the sense layer - that is the
                     ONLY way ON CLUSTER is ever emitted (a named topology from
                     config cannot express it). When omitted, the engine is
                     resolved from ``DDLConfig.topology`` alone, which suits the
                     static/offline paths (reference SQL, gitops artefacts) that
                     have no server to introspect.
        """
        self._registry = registry
        self._index_templates = _INDEX_TEMPLATES_LEGACY if use_legacy_indexes else _INDEX_TEMPLATES
        self._key_search_templates = (
            _KEY_SEARCH_TEMPLATES_LEGACY if use_legacy_indexes else _KEY_SEARCH_TEMPLATES
        )
        self._resolver = resolver

    # ── engine resolution ───────────────────────────────────────────

    def _resolve_engine(self, cfg: DDLConfig) -> ResolvedEngine:
        """Resolve the engine for *cfg*, preferring an injected resolver.

        The injected resolver carries whatever cascade inputs the caller has
        (notably a live client, which is what enables sensing). Falling back to
        ``DDLConfig.topology`` as an override keeps the static/offline callers -
        reference SQL, gitops artefacts - rendering exactly as they always have.
        """
        spec = parse_engine(cfg.engine)
        resolver = self._resolver or EngineResolver(override=cfg.topology)
        return resolver.resolve(spec, cfg.db)

    def _on_cluster(self, cfg: DDLConfig) -> str:
        """The ``ON CLUSTER`` suffix for any statement, or "".

        An explicit ``cfg.cluster`` pin wins, else the resolver decides -- the
        same order ``generate_create_table`` uses. Every statement that changes
        a table's definition needs this, not just the CREATE: an ALTER or a
        CREATE VIEW without it applies to the ONE node the connection landed on,
        and the siblings behind a headless Service silently diverge. Without an
        injected resolver this is always "" (a named topology from config
        carries no ON CLUSTER intent), so the offline render paths are unchanged.
        """
        if cfg.cluster:
            return f" ON CLUSTER {cfg.cluster}"
        return self._resolve_engine(cfg).on_cluster

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

        # Resolve the engine first: it decides BOTH the ENGINE clause below and
        # whether this table needs ON CLUSTER on the header.
        resolved = self._resolve_engine(cfg)

        # Header comment
        lines.extend(
            [line for line in self._build_schema_header(cfg, table_name, generated_time) if line]
        )

        # CREATE TABLE. ON CLUSTER comes from an explicit cfg.cluster pin, else
        # from the resolver - which only ever senses it from a live server (a
        # named topology from config carries no ON CLUSTER intent). Without it a
        # Replicated table is created on the ONE node the connection landed on,
        # and the siblings behind a headless service silently diverge.
        create = f"CREATE TABLE IF NOT EXISTS {_qualified(cfg.db, table_name, what='table name')}"
        if cfg.cluster:
            create += f" ON CLUSTER {cfg.cluster}"
        else:
            create += resolved.on_cluster
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
        # The resolver genericises beyond plain MergeTree - ANY family variant with
        # its params (ReplacingMergeTree(ver), SummingMergeTree(cols), ...) renders
        # correctly: single -> <variant>(params); replicated -> argumentless
        # Replicated<variant>(params) (no double-parens, no dropped ver).
        lines.append(f")\nENGINE = {resolved.clause}")

        # PARTITION BY -- a raw expression wins over the column + granularity.
        partition = None
        if cfg.partition_by:
            partition = cfg.partition_by
        elif any(column for column in columns if column.name == cfg.partition_column):
            partition = self._partition_expr(cfg)
        if partition:
            lines.append(f"PARTITION BY {partition}")

        # ORDER BY + PRIMARY KEY. A raw ORDER BY sets no PRIMARY KEY of its own:
        # ClickHouse then takes the sorting key as the primary key, which is what
        # a table declaring only an ORDER BY expects.
        if cfg.order_by:
            if cfg.primary_key:
                lines.append(f"PRIMARY KEY ({cfg.primary_key})")
            lines.append(f"ORDER BY ({cfg.order_by})")
        else:
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
        if ttl and cfg.ttl_only_drop_parts:
            settings_parts.append(
                f"ttl_only_drop_parts = {int(self._ttl_drops_whole_parts(cfg, partition))}"
            )
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
        sql = (
            f"ALTER TABLE {_qualified(cfg.db, table_name, what='table name')}"
            f"{self._on_cluster(cfg)} ADD COLUMN IF NOT EXISTS {col_def}"
        )
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
        return (
            f"ALTER TABLE {_qualified(cfg.db, table_name, what='table name')}"
            f"{self._on_cluster(cfg)} MODIFY COLUMN {col_def};\n"
        )

    def generate_alter_add_indexes(
        self,
        table_name: str,
        column: SchemaColumn,
        config: DDLConfig | None = None,
    ) -> list[str]:
        """Generate ALTER TABLE ADD INDEX for a column's declared index or use_case.

        Empty when the column declares neither, and two statements for a
        ``key_search`` column, which is indexed on its keys and its values apart.
        """
        cfg = config or DDLConfig()
        qualified = _qualified(cfg.db, table_name, what="table name")
        on_cluster = self._on_cluster(cfg)
        return [
            f"ALTER TABLE {qualified}{on_cluster} ADD {idx};\n" for idx in self._index_defs(column)
        ]

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
            f"CREATE OR REPLACE VIEW {_qualified(cfg.db, view_name, what='view name')}"
            f"{self._on_cluster(cfg)} AS\n"
            f"SELECT\n"
            f"{select_parts}\n"
            f"FROM {_qualified(cfg.db, table_name, what='table name')};\n"
        )

    def generate_sigma_view(
        self,
        table_name: str,
        mappings: dict[str, str],
        config: DDLConfig | None = None,
    ) -> str:
        """Generate Sigma view (convenience wrapper for generate_view)."""
        return self.generate_view(table_name, mappings, "sigma", config)

    # ── Internal: PARTITION BY ──────────────────────────────────────

    _PARTITION_FUNCS = {"day": "toYYYYMMDD", "month": "toYYYYMM"}

    @classmethod
    def _partition_expr(cls, cfg: DDLConfig) -> str:
        """Build the PARTITION BY expression for *cfg*.

        Raises:
            DDLGenerationError: Unknown partition granularity.
        """
        func = cls._PARTITION_FUNCS.get(cfg.partition_granularity)
        if func is None:
            raise DDLGenerationError(
                f"unknown partition_granularity {cfg.partition_granularity!r}; "
                f"valid: {', '.join(sorted(cls._PARTITION_FUNCS))}"
            )
        return f"{func}({cfg.partition_column})"

    # ── Internal: body lines ────────────────────────────────────────

    def _build_schema_header(
        self, cfg: DDLConfig, table_name: str, generated_at: str | None = None
    ) -> list[str]:
        """Build the comment header.

        Carries no engine version: dfe-schemas commits this output and
        diff-checks it against a fresh render, so a version stamp would report
        drift on every dfe-engine release.
        """
        return [
            "-- =============================================================================",
            f"-- DFE Schema DDL: {table_name}",
            f"-- Profile: {cfg.profile_name}" if cfg.profile_name else "",
            f"-- {cfg.description}" if cfg.description else "",
            "-- Generated by: dfe-engine DDLFileWriter",
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
            body.extend(f"    {idx}" for idx in self._index_defs(col))

        # Raw index definitions, for shapes the use_case templates cannot express.
        for idx_def in cfg.extra_indexes:
            body.append(f"    {idx_def}")

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

        ch_type = _with_max_dynamic_paths(resolved.ch_type, col.max_dynamic_paths)
        return ch_type, codec

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

    def _index_defs(self, col: SchemaColumn) -> list[str]:
        """The INDEX definitions a column asks for -- none, one, or two.

        A column declaring its own ``index`` is emitted verbatim: the common
        header asks ``_raw`` for ``text(tokenizer = 'default') GRANULARITY 64``,
        which no use_case template expresses, and a template rendered instead
        drifts the table away from the schema it was generated from.
        """
        index_name = f"idx_{col.name}"
        quoted = f"`{col.name}`"
        if col.index:
            return [f"INDEX {index_name} {quoted} TYPE {col.index}"]

        use_case, dims = split_use_case(col.use_case)
        if use_case is None:
            return []

        if use_case == "key_search":
            return [
                template.format(name=f"idx_{col.name}_{suffix}", col=quoted)
                for suffix, template in self._key_search_templates
            ]

        if use_case == "similarity_search":
            if dims is None:
                raise InvalidUseCaseError(
                    f"column {col.name!r}: similarity_search needs the vector "
                    f"dimension count, as similarity_search(<dims>)"
                )
            return [_SIMILARITY_SEARCH_TEMPLATE.format(name=index_name, col=quoted, dims=dims)]

        if use_case == "exact_match":
            template = (
                _EXACT_MATCH_LOW_CARDINALITY
                if "lowcardinality" in col.attribute
                else _EXACT_MATCH_HIGH_CARDINALITY
            )
            return [template.format(name=index_name, col=quoted)]

        template = self._index_templates.get(use_case)
        if template is None:
            return []
        return [template.format(name=index_name, col=quoted)]

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
    def _ttl_drops_whole_parts(cfg: DDLConfig, partition: str | None) -> bool:
        """Whether every TTL column is carried by the partition expression.

        ``ttl_only_drop_parts`` waits for EVERY row in a part to expire, so on a
        table the TTL is not aligned to it silently retains data forever; those
        tables need the row-level delete instead.
        """
        if not partition:
            return False
        return all(col in partition for col in cfg.ttl_columns)

    @staticmethod
    def _ttl_clause(cfg: DDLConfig, columns: list[SchemaColumn]) -> str | None:
        """Build the TTL clause.

        Raises:
            DDLGenerationError: Retention was declared over columns the table does
                not carry, which would otherwise render as a table that silently
                keeps everything forever.
        """
        if not (cfg.ttl_days):
            return None
        if not cfg.ttl_columns:
            raise DDLGenerationError(
                f"ttl_days={cfg.ttl_days} declared with no ttl_columns to apply it to"
            )

        present = {column.name for column in columns}
        missing = [col for col in cfg.ttl_columns if col not in present]
        if missing:
            raise DDLGenerationError(
                f"ttl_days={cfg.ttl_days} declared over absent column(s) "
                f"{', '.join(missing)}; the table would keep every row forever"
            )

        parts = [
            f"{col} + INTERVAL {cfg.ttl_days} DAY DELETE WHERE {col} >= 0"
            for col in cfg.ttl_columns
        ]
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
