"""DDL generator -- ClickHouse DDL from SchemaColumn models + TypeRegistry.

Uses TypeRegistry for primitive->CH type resolution and generates
complete CREATE TABLE statements.

Usage:
    from dfe_engine.schema.schema_ddl import DDLGenerator, DDLConfig
    from dfe_engine.source.type_registry import TypeRegistry

    registry = TypeRegistry.default()
    gen = DDLGenerator(registry)
    ddl = gen.generate_create_table("filebeat", columns, DDLConfig(ttl_days=90))
"""

import functools
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from dfe_schemas.clickhouse import Platform
from dfe_schemas.ddl_catalogue import CATALOGUE_FILE, TableDefaults
from dfe_schemas.loader import Column
from dfe_schemas.loader import SchemaError as DefinitionError
from dfe_schemas.render import TableDDL
from dfe_schemas.render import TableSpec as CreateSpec
from scalo.logger import logger

from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import InvalidUseCaseError, TypeRegistry


class DDLGenerationError(Exception):
    """Error generating DDL."""


def unsafe_ident_reason(name: str) -> str | None:
    r"""Why *name* is unsafe in a DDL identifier position, or None when safe.

    THE one charset rule for operator-suppliable identifiers, shared by every
    DDL sink (the view mappings here, ``fieldmap.remap_view``): empty, any of
    the control chars ``\` ; ( ) ' " \\``, or whitespace is rejected - each
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


# A name ClickHouse reads as one identifier without backticks.
_BARE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ident(name: str, *, what: str = "index name") -> str:
    """A name bare when ClickHouse reads it bare, else quoted through :func:`quote_ident`.

    For the positions the renderer has always written bare: index and projection
    names and TTL columns. A safe name stays bare so the
    DDL text of a table already created does not change. A name taken
    from a dotted column such as ``event.action`` is one identifier only once
    quoted, and an unsafe one is refused.
    """
    if _BARE_IDENT.fullmatch(name):
        return name
    return quote_ident(name, what=what)


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
    # The MergeTree-family variant. dfe-schemas renders it as the argumentless
    # Replicated form every DFE table takes, on every platform.
    engine: str = "MergeTree"
    # Retention is DECLARED, in dfe-schemas or a source's config, never defaulted
    # here: a default no operator can see is how a table ends up keeping
    # everything or dropping what it should not. None is undeclared; 0 declares no TTL.
    ttl_days: int | None = None
    # TTL rides the partition column alone: ttl_only_drop_parts drops a part only
    # once every row in it has expired, so a second rule over event time can only
    # ever delay the drop.
    ttl_columns: list[str] = field(default_factory=lambda: [table_defaults().partition_column])
    partition_column: str = field(default_factory=lambda: table_defaults().partition_column)
    # A key into the catalogue's partition functions. A low-volume audit table
    # wants month, else it accumulates a part per day holding very few rows.
    partition_granularity: str = field(
        default_factory=lambda: table_defaults().partition_granularity
    )
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
    index_granularity: int = field(default_factory=lambda: table_defaults().index_granularity)
    ttl_only_drop_parts: bool = True
    sample_by: str | None = None
    # Off unless asked for: a SELECT * projection copies every column, so on a JSON-heavy
    # table it doubles the substream metadata ClickHouse holds in memory.
    projection_order_by: str | None = None
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


@functools.cache
def _tables_at(root: Path, platform: Platform) -> TableDDL:
    return TableDDL.load(platform=platform, root=root)


def table_ddl(platform: Platform = Platform.SELF_HOSTED) -> TableDDL:
    """dfe-schemas' table renderer, over the tree that carries ``registries/ddl.yaml``.

    Every column, index, partition and TTL clause the engine renders comes from
    it. There is no built-in fallback: a tree without the catalogue fails here,
    naming every tree it searched.

    Raises:
        SchemaLoadError: No dfe-schemas tree carries the catalogue.
    """
    # Imported here because schema_loader's own imports reach back into this module.
    from dfe_engine.schema.schema_loader import resolve_registry_path

    return _tables_at(resolve_registry_path(CATALOGUE_FILE).parents[1], platform)


def table_defaults() -> TableDefaults:
    """The table defaults in dfe-schemas' DDL catalogue."""
    return table_ddl().catalogue.table


class DDLGenerator:
    """Generates ClickHouse DDL from SchemaColumn models.

    Turns a source's columns and config into a dfe-schemas ``TableSpec`` and
    renders it through dfe-schemas' ``TableDDL``, the table renderer the
    manifest tables use. The engine adds only the header comment and the views.
    """

    def __init__(
        self,
        registry: TypeRegistry,
        *,
        platform: Platform = Platform.SELF_HOSTED,
        tables: TableDDL | None = None,
    ) -> None:
        """Initialize the DDL generator.

        Args:
            registry: TypeRegistry the columns are validated against.
            platform: The ClickHouse platform the DDL is for, which picks a JSON
                     table's platform-only settings. A live caller passes what its
                     connection's probe names. Self-hosted, the probe's own
                     default, suits the offline paths (reference SQL, gitops
                     artefacts) that have no server to ask.
            tables: The table renderer. Defaults to :func:`table_ddl` for *platform*.
        """
        self._registry = registry
        self._platform = platform
        self._tables = tables or table_ddl(platform)

    # -- CREATE TABLE ------------------------------------------------

    def generate_create_table(
        self,
        table_name: str,
        columns: list[SchemaColumn],
        config: DDLConfig | None = None,
        generated_time: str | None = None,
    ) -> str:
        """Generate a complete CREATE TABLE IF NOT EXISTS statement.

        No ON CLUSTER: the Replicated database replicates the CREATE, and
        refuses ON CLUSTER on a table inside it (code 80).

        Args:
            table_name: ClickHouse table name.
            columns: Schema columns (profile header + source-specific).
            config: DDL configuration. Defaults to DDLConfig().
            generated_time: Value for the header's ``Generated at`` line, which
                is left out when None.

        Returns:
            Complete CREATE TABLE DDL string.

        Raises:
            DDLGenerationError: A name, clause or column cannot be rendered.
        """
        cfg = config or DDLConfig()
        header = [
            line for line in self._build_schema_header(cfg, table_name, generated_time) if line
        ]
        spec = self._spec(table_name, columns, cfg)
        try:
            create = self._tables.create_table(spec)
        except DefinitionError as exc:
            raise DDLGenerationError(str(exc)) from exc
        return "\n".join([*header, create]) + ";\n"

    def _spec(self, table_name: str, columns: list[SchemaColumn], cfg: DDLConfig) -> CreateSpec:
        """The dfe-schemas spec for one source table.

        Design decision: a Nullable ORDER BY column is dropped from the sorting
        key with a warning, never refused. ClickHouse refuses a Nullable key
        unless allow_nullable_key=1, and even then a null in the key cripples
        the index and doubles storage. Mark the column 'not_null' (or give it a
        DEFAULT) to keep it in the key. See docs/data-plane/schema.md
        "Nullability Defaults".
        """
        _qualified(cfg.db, table_name, what="table name")
        for col in cfg.ttl_columns:
            quote_ident(col, what="TTL column")
        mapped = [self._column(col) for col in columns]
        for column in mapped:
            self._indexes_for(column)
        nullable_keys = set(self._tables.nullable_key_columns(mapped))
        for name in sorted(nullable_keys):
            logger.warning(
                f"Column {name!r} resolves to a Nullable type and is marked as an "
                f"ORDER BY key; skipping it from the sorting key. ClickHouse keys "
                f"cannot be Nullable (requires allow_nullable_key and hurts index "
                f"performance). Add the 'not_null' attribute or a DEFAULT to keep it "
                f"in the key."
            )
        keyed = tuple(
            replace(column, order=None) if column.name in nullable_keys else column
            for column in mapped
        )
        return CreateSpec(
            database=cfg.db,
            name=table_name,
            columns=keyed,
            engine=cfg.engine,
            ttl_days=cfg.ttl_days,
            ttl_columns=tuple(cfg.ttl_columns),
            ttl_only_drop_parts=cfg.ttl_only_drop_parts,
            partition_by=cfg.partition_by,
            partition_column=cfg.partition_column,
            partition_granularity=cfg.partition_granularity,
            order_by=cfg.order_by,
            primary_key=cfg.primary_key,
            sample_by=cfg.sample_by,
            extra_indexes=tuple(cfg.extra_indexes),
            index_granularity=cfg.index_granularity,
            projection_order_by=cfg.projection_order_by,
            comment=self._table_comment(table_name, cfg),
        )

    # -- ALTER TABLE -------------------------------------------------

    def generate_alter_add_column(
        self, table_name: str, column: SchemaColumn, config: DDLConfig | None = None
    ) -> str:
        """Generate ALTER TABLE ADD COLUMN for one column."""
        qualified = _qualified((config or DDLConfig()).db, table_name, what="table name")
        return f"ALTER TABLE {qualified} ADD COLUMN IF NOT EXISTS {self._column_def(column)};\n"

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
        qualified = _qualified((config or DDLConfig()).db, table_name, what="table name")
        return [f"ALTER TABLE {qualified} ADD {idx};\n" for idx in self._index_defs(column)]

    # -- Standard Views ---------------------------------------------

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
            mappings: standard_field -> column_name mapping.
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
            f"CREATE OR REPLACE VIEW {_qualified(cfg.db, view_name, what='view name')} AS\n"
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

    # -- Internal: header comment ------------------------------------

    def _build_schema_header(
        self, cfg: DDLConfig, table_name: str, generated_at: str | None = None
    ) -> list[str]:
        """Build the comment header.

        Carries no engine version, so a dfe-engine release does not change the
        DDL text of a table whose definition has not changed.
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

    # -- Internal: columns -------------------------------------------

    def _column(self, col: SchemaColumn) -> Column:
        """The dfe-schemas column for *col*, after the engine's own checks.

        An enum with no ``ch_override`` takes ``Enum8(<default>)`` and keeps the
        enum primitive's codec, which an explicit ``ch_override`` would drop.

        Raises:
            DDLGenerationError: The column's name is unsafe in DDL, or its DEFAULT
                reads outside the row.
            TypeRegistryError: The column's type, attribute or override is not
                in the registry.
        """
        quote_ident(col.name, what="column name")
        refusals = col.offbox_default_errors()
        if refusals:
            raise DDLGenerationError(refusals[0])
        self._registry.resolve(
            col.type,
            attributes=col.attribute,
            ch_override=col.ch_override,
            cardinality=col.declared_cardinality,
        )
        ch_override = col.ch_override
        codec = col.codec
        default = col.default
        if col.type == "enum" and not ch_override:
            if col.default:
                ch_override = f"Enum8({col.default})"
            else:
                logger.warning(
                    f"Column {col.name!r}: enum type without ch_override "
                    f"or default values -- using Enum8('') placeholder"
                )
                ch_override = "Enum8('')"
            codec = codec or self._registry.primitive_defaults(col.type).get("codec")
        return Column(
            name=col.name,
            primitive=col.type,
            ch_override=ch_override,
            attribute=tuple(col.attribute),
            cardinality=col.declared_cardinality,
            use_case=col.use_case,
            order=col.order,
            default=default,
            codec=codec,
            comment=col.comment,
            expr=col.expr,
            max_dynamic_paths=col.max_dynamic_paths,
            index=col.index,
        )

    def _column_def(self, col: SchemaColumn) -> str:
        """One column's definition, rendered by dfe-schemas."""
        return self._tables.column_def(self._column(col))

    def resolve_type(self, col: SchemaColumn) -> tuple[str, str | None]:
        """Resolve column to (ch_type, codec).

        Raises:
            TypeRegistryError: The column's type, attribute or override is not in the registry.
        """
        resolved = self._tables.resolve(self._column(col))
        return resolved.ch_type, resolved.codec

    def json_settings(self, columns: list[SchemaColumn]) -> dict[str, int]:
        """The table SETTINGS dfe-schemas declares for a table with a JSON column on this platform.

        Empty when no column resolves to JSON. The CREATE emits these and the
        applier reconciles them onto a table that exists, so both read this one rule.

        Raises:
            TypeRegistryError: A column's type, attribute or override is not in the registry.
        """
        return self._tables.json_settings([self._column(col) for col in columns])

    def _index_defs(self, col: SchemaColumn) -> list[str]:
        """The INDEX definitions a column asks for -- none, one, or two.

        A column declaring its own ``index`` is emitted verbatim: that is a
        shape no use case answers.

        Raises:
            InvalidUseCaseError: The use case is unknown, or lacks an argument it needs.
        """
        return self._indexes_for(self._column(col))

    def _indexes_for(self, column: Column) -> list[str]:
        try:
            return self._tables.index_defs(column)
        except DefinitionError as exc:
            raise InvalidUseCaseError(str(exc)) from exc

    # -- Internal: TTL -----------------------------------------------

    @staticmethod
    def ttl_clause(cfg: DDLConfig, columns: list[SchemaColumn]) -> str | None:
        """Build the TTL clause, or None when ``cfg.ttl_days`` declares no TTL.

        Raises:
            DDLGenerationError: Retention was declared over columns the table does
                not carry, which would otherwise render as a table that silently
                keeps everything forever, or a TTL column's name is unsafe in DDL.
        """
        if not cfg.ttl_days:
            return None
        for col in cfg.ttl_columns:
            quote_ident(col, what="TTL column")
        generator = DDLGenerator(TypeRegistry.default())
        spec = CreateSpec(
            database=cfg.db,
            name="table",
            columns=tuple(generator._column(col) for col in columns),
            ttl_days=cfg.ttl_days,
            ttl_columns=tuple(cfg.ttl_columns),
        )
        try:
            return generator._tables.ttl_clause(spec)
        except DefinitionError as exc:
            raise DDLGenerationError(str(exc)) from exc

    # -- Internal: table comment -------------------------------------

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
