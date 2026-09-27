"""Rule SQL rewriter -- transform user SQL for hunt execution.

Takes a user-supplied SQL SELECT statement (typically from HyperDX)
and rewrites it for hunt execution:

1. Detect and reject SELECT * (or rewrite to lean columns)
2. Strip time-bound conditions from WHERE clause
3. Extract the detection logic (WHERE clause without time bounds)

The rewriter does NOT generate the final INSERT INTO ... SELECT --
that's done by HuntResultSchema.build_insert_select(). This module
handles the parsing/cleaning of user-supplied SQL.

Usage:
    from dfe_engine.hunts.rule_rewriter import RuleRewriter

    rewriter = RuleRewriter()
    result = rewriter.parse_user_sql(
        "SELECT * FROM acme.windows_audit "
        "WHERE timestamp > '2026-01-01' AND process_executable = 'certutil.exe'"
    )
    # result.source_table == "windows_audit"
    # result.where_clause == "process_executable = 'certutil.exe'"
    # result.had_select_star == True
    # result.stripped_time_bounds == ["timestamp > '2026-01-01'"]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Common timestamp column patterns in DFE tables.
_TIMESTAMP_COLUMNS = frozenset(
    {
        "timestamp",
        "_timestamp",
        "_timestamp_load",
        "timestamp_load",
        "@timestamp",
    }
)


def _build_time_bound_patterns(columns: frozenset[str]) -> list[re.Pattern]:
    """Build regex patterns for time-bound conditions from column names."""
    cols = "|".join(re.escape(c) for c in columns)
    return [
        re.compile(
            rf"""\b({cols})\s*(?:>=?|<=?|=)\s*'[^']*'""",
            re.IGNORECASE,
        ),
        re.compile(
            rf"""\b({cols})\s*(?:>=?|<=?)\s*now\s*\(\)\s*-\s*INTERVAL\s+\S+(?:\s+\S+)?""",
            re.IGNORECASE,
        ),
        re.compile(
            rf"""\b({cols})\s+BETWEEN\s+'[^']*'\s+AND\s+'[^']*'""",
            re.IGNORECASE,
        ),
        re.compile(
            rf"""\b({cols})\s*(?:>=?|<=?)\s*\{{\{{[^}}]*\}}\}}""",
            re.IGNORECASE,
        ),
    ]


# Default patterns for module-level use.
_TIME_BOUND_PATTERNS = _build_time_bound_patterns(_TIMESTAMP_COLUMNS)

# SELECT * detection
_SELECT_STAR_RE = re.compile(
    r"\bSELECT\s+\*\s+FROM\b",
    re.IGNORECASE,
)

# Extract source table from FROM clause
_FROM_TABLE_RE = re.compile(
    r"\bFROM\s+(?:(\w+)\.)?(\w+)\b",
    re.IGNORECASE,
)

# Extract WHERE clause
_WHERE_RE = re.compile(
    r"\bWHERE\s+(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|;|\Z)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class ParsedRule:
    """Result of parsing a user-supplied SQL statement."""

    source_db: str | None = None
    source_table: str | None = None
    where_clause: str = ""
    had_select_star: bool = False
    stripped_time_bounds: list[str] = field(default_factory=list)
    original_sql: str = ""
    warnings: list[str] = field(default_factory=list)


class RuleRewriter:
    """Parse and clean user-supplied SQL for hunt rule creation.

    The rewriter extracts the detection logic from a user's SQL SELECT,
    stripping time bounds and SELECT * patterns. The cleaned output
    is then passed to HuntResultSchema.build_insert_select() for
    final SQL generation.
    """

    def __init__(
        self,
        timestamp_columns: frozenset[str] | None = None,
    ):
        """Initialize the rewriter.

        Args:
            timestamp_columns: Additional timestamp column names to
                strip from WHERE clauses. Merged with the default set.
        """
        if timestamp_columns:
            self._ts_columns = _TIMESTAMP_COLUMNS | timestamp_columns
            self._patterns = _build_time_bound_patterns(self._ts_columns)
        else:
            self._ts_columns = _TIMESTAMP_COLUMNS
            self._patterns = _TIME_BOUND_PATTERNS

    def parse_user_sql(self, sql: str) -> ParsedRule:
        """Parse a user-supplied SQL SELECT statement.

        Extracts the source table, WHERE clause (with time bounds
        removed), and flags whether SELECT * was used.

        Args:
            sql: User-supplied SQL SELECT statement.

        Returns:
            ParsedRule with extracted components.
        """
        result = ParsedRule(original_sql=sql.strip())

        # Detect SELECT *
        result.had_select_star = bool(_SELECT_STAR_RE.search(sql))
        if result.had_select_star:
            result.warnings.append(
                "SELECT * detected -- hunt output will use lean columns "
                "(matched_uuid + rule metadata + _json) instead."
            )

        # Extract source table
        from_match = _FROM_TABLE_RE.search(sql)
        if from_match:
            result.source_db = from_match.group(1)
            result.source_table = from_match.group(2)
        else:
            result.warnings.append("Could not extract source table from SQL.")

        # Extract WHERE clause
        where_match = _WHERE_RE.search(sql)
        if where_match:
            raw_where = where_match.group(1).strip()
            result.where_clause = self._strip_time_bounds(raw_where, result.stripped_time_bounds)
        else:
            result.warnings.append("No WHERE clause found in SQL.")

        return result

    def _strip_time_bounds(self, where_clause: str, stripped: list[str]) -> str:
        """Remove time-bound conditions from a WHERE clause.

        Args:
            where_clause: Original WHERE clause text.
            stripped: List to append stripped conditions to.

        Returns:
            WHERE clause with time bounds removed.
        """
        result = where_clause

        for pattern in self._patterns:
            for match in pattern.finditer(result):
                stripped.append(match.group(0))
            result = pattern.sub("", result)

        # Clean up residual AND/OR operators
        result = self._clean_boolean_operators(result)

        return result.strip()

    @staticmethod
    def _clean_boolean_operators(clause: str) -> str:
        """Remove dangling AND/OR from stripped conditions."""
        # Remove leading AND/OR
        clause = re.sub(r"^\s*(?:AND|OR)\s+", "", clause, flags=re.IGNORECASE)
        # Remove trailing AND/OR
        clause = re.sub(r"\s+(?:AND|OR)\s*$", "", clause, flags=re.IGNORECASE)
        # Remove doubled AND/OR (from middle removal)
        clause = re.sub(r"\s+AND\s+AND\s+", " AND ", clause, flags=re.IGNORECASE)
        clause = re.sub(r"\s+OR\s+OR\s+", " OR ", clause, flags=re.IGNORECASE)
        # Remove AND/OR next to parentheses: ( AND ... or ... AND )
        clause = re.sub(r"\(\s*(?:AND|OR)\s+", "(", clause, flags=re.IGNORECASE)
        clause = re.sub(r"\s+(?:AND|OR)\s*\)", ")", clause, flags=re.IGNORECASE)
        # Remove empty parentheses
        clause = re.sub(r"\(\s*\)", "", clause)
        return clause.strip()
