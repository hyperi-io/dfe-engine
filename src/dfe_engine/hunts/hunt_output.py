"""Hunt output schema -- dynamic column generation from common header profile.

The hunt results table schema is composed from two sources:
1. Common header columns (loaded from a profile, default 'timeseries')
2. Hunt-specific detection columns (matched_uuid, rule metadata)

This replaces hardcoded INSERT/SELECT templates with a schema-driven
approach. The output is always lean: common header columns + matched record
UUID + rule metadata + _json copy of the matched record.

Usage:
    from dfe_engine.hunts.hunt_output import HuntResultSchema

    schema = HuntResultSchema()  # loads timeseries profile
    insert_cols = schema.insert_columns()
    select_expr = schema.select_expression(
        rule_id="win_priv_esc_01",
        rule_name="Windows Privilege Escalation",
        source_table="windows_audit",
        hunt_name="windows_detection_hunt",
        severity="high",
    )
"""

from dataclasses import dataclass

from scalo.logger import logger

from ..clickhouse.quoting import quote_identifier, quote_literal
from ..schema.schema_loader import SchemaLoader

# Common header columns to copy from source -> hunt results.
# These are the columns we SELECT from the source table.
# Other profile columns (like _raw, _tags) are NOT copied --
# the full record is available via _json.
_HEADER_COLUMNS_TO_COPY = frozenset(
    {
        "_timestamp",
        "_timestamp_load",
        "_org_id",
    }
)

# Columns that exist on the hunt results table but are generated
# by ClickHouse defaults (not from the source SELECT).
_HEADER_COLUMNS_GENERATED = frozenset(
    {
        "_uuid",  # generateUUIDv7() -- new UUID for the hunt result row
        "_source",  # set by the hunt engine to identify the hunt
    }
)


@dataclass(frozen=True)
class HuntDetectionColumn:
    """A hunt-specific detection column."""

    name: str
    select_expr: str | None = None  # None = literal value injected at query time
    ch_type: str = "LowCardinality(String)"
    codec: str = "ZSTD(1)"
    comment: str = ""


# Hunt-specific columns appended after common header.
HUNT_DETECTION_COLUMNS: tuple[HuntDetectionColumn, ...] = (
    HuntDetectionColumn(
        name="matched_uuid",
        select_expr="_uuid",
        ch_type="UUID",
        codec="ZSTD(1)",
        comment="UUID of the matching source record",
    ),
    HuntDetectionColumn(
        name="rule_id",
        comment="Rule identifier",
    ),
    HuntDetectionColumn(
        name="rule_name",
        comment="Human-readable rule name",
    ),
    HuntDetectionColumn(
        name="source_table",
        comment="Source table that was scanned",
    ),
    HuntDetectionColumn(
        name="hunt_name",
        comment="Parent hunt name",
    ),
    HuntDetectionColumn(
        name="severity",
        comment="Detection severity",
    ),
)


# Column names present on the hunt results table -- used by alert grouping
# to distinguish direct column references from _json field extraction.
RESULTS_TABLE_COLUMNS: frozenset[str] = (
    _HEADER_COLUMNS_TO_COPY
    | _HEADER_COLUMNS_GENERATED
    | frozenset(dc.name for dc in HUNT_DETECTION_COLUMNS)
    | frozenset({"_json"})
)


class HuntResultSchema:
    """Dynamic hunt results schema composed from common header + detection columns.

    Loads the common header profile (default: timeseries) and builds
    column lists for INSERT and SELECT operations. The _json column
    is always included for a full copy of the matched record.

    The schema is driven by the profile YAML, not hardcoded -- if the
    common header changes, hunt output adapts automatically.
    """

    def __init__(
        self,
        profile_name: str = "timeseries",
        profiles_dir: str | None = None,
        include_json_copy: bool = True,
    ):
        self._profile_name = profile_name
        self._include_json_copy = include_json_copy
        self._profile_columns = SchemaLoader.load_profile(profile_name, profiles_dir=profiles_dir)
        self._header_copy_columns = [
            col for col in self._profile_columns if col.name in _HEADER_COLUMNS_TO_COPY
        ]
        self._has_json = any(col.name == "_json" for col in self._profile_columns)

        if include_json_copy and not self._has_json:
            logger.warning(
                f"Profile '{profile_name}' has no _json column. "
                "Hunt results will not include a JSON copy of matched records."
            )

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def detection_columns(self) -> tuple[HuntDetectionColumn, ...]:
        return HUNT_DETECTION_COLUMNS

    def insert_columns(self) -> list[str]:
        """Column names for the INSERT INTO clause.

        Returns the ordered list of columns that the INSERT statement
        should target. Generated columns (_uuid, _source) are excluded
        since ClickHouse fills those via DEFAULT.
        """
        cols = [col.name for col in self._header_copy_columns]
        cols.extend(dc.name for dc in HUNT_DETECTION_COLUMNS)
        if self._include_json_copy and self._has_json:
            cols.append("_json")
        return cols

    def select_expression(
        self,
        *,
        rule_id: str,
        rule_name: str,
        source_table: str,
        hunt_name: str,
        severity: str,
    ) -> str:
        """Build the SELECT column expression for a hunt rule.

        Generates a SELECT clause that produces the lean output:
        common header columns from the source, rule metadata as
        literals, and optionally _json for the full record copy.

        Args:
            rule_id: Rule identifier.
            rule_name: Human-readable rule name.
            source_table: Source table being scanned.
            hunt_name: Parent hunt name.
            severity: Detection severity level.

        Returns:
            SELECT expression string (without the SELECT keyword).
        """
        parts: list[str] = []

        # Common header columns from source
        for col in self._header_copy_columns:
            parts.append(col.name)

        # Hunt detection columns
        literals = {
            "rule_id": rule_id,
            "rule_name": rule_name,
            "source_table": source_table,
            "hunt_name": hunt_name,
            "severity": severity,
        }
        for dc in HUNT_DETECTION_COLUMNS:
            if dc.select_expr:
                parts.append(f"{dc.select_expr} AS {dc.name}")
            else:
                parts.append(f"{quote_literal(literals[dc.name])} AS {dc.name}")

        # JSON copy of the full matched record
        if self._include_json_copy and self._has_json:
            parts.append("_json")

        return ",\n    ".join(parts)

    def build_insert_select(
        self,
        *,
        target_db: str,
        target_table: str,
        source_db: str,
        source_table: str,
        where_clause: str,
        rule_id: str,
        rule_name: str,
        hunt_name: str,
        severity: str,
        timestamp_placeholder: str = "{timestamp_condition}",
    ) -> str:
        """Build a complete INSERT INTO ... SELECT statement.

        This is the primary method for generating hunt rule SQL.
        The WHERE clause should contain the detection logic only --
        time bounds are injected via the timestamp placeholder.

        Args:
            target_db: Target database (usually org_id), backtick-quoted here.
            target_table: Hunt results table name, backtick-quoted here.
            source_db: Source database (usually org_id), backtick-quoted here.
            source_table: Source table to scan, backtick-quoted here.
            where_clause: Detection logic (without time bounds).
            rule_id: Rule identifier.
            rule_name: Human-readable rule name.
            hunt_name: Parent hunt name.
            severity: Detection severity level.
            timestamp_placeholder: Placeholder for checkpoint time bounds.

        Returns:
            Complete SQL statement string.
        """
        insert_cols = ", ".join(self.insert_columns())
        select_expr = self.select_expression(
            rule_id=rule_id,
            rule_name=rule_name,
            source_table=source_table,
            hunt_name=hunt_name,
            severity=severity,
        )

        # Compose WHERE: timestamp condition AND detection logic
        where_parts = [timestamp_placeholder]
        if where_clause.strip():
            where_parts.append(f"({where_clause.strip()})")
        full_where = " AND ".join(where_parts)

        # Source and target are hunt configuration text, so each can only ever name a table.
        target = f"{quote_identifier(target_db)}.{quote_identifier(target_table)}"
        source = f"{quote_identifier(source_db)}.{quote_identifier(source_table)}"
        return (
            f"INSERT INTO {target}\n"
            f"    ({insert_cols})\n"
            f"SELECT\n"
            f"    {select_expr}\n"
            f"FROM {source}\n"
            f"WHERE {full_where}"
        )

    def result_table_columns(self) -> list[dict]:
        """Return column definitions for the hunt results table.

        Returns a list of dicts suitable for DDL generation or
        schema documentation. Includes common header, detection
        columns, and optional _json.
        """
        columns = []

        # Full profile columns (including generated ones like _uuid)
        for col in self._profile_columns:
            # Skip _raw and _tags -- not needed in hunt results
            if col.name in ("_raw", "_tags"):
                continue
            columns.append(
                {
                    "name": col.name,
                    "type": col.type,
                    "attribute": col.attribute,
                    "default": col.default,
                    "expr": col.expr,
                    "comment": col.comment,
                }
            )

        # Hunt detection columns
        for dc in HUNT_DETECTION_COLUMNS:
            columns.append(
                {
                    "name": dc.name,
                    "ch_type": dc.ch_type,
                    "codec": dc.codec,
                    "comment": dc.comment,
                }
            )

        return columns
