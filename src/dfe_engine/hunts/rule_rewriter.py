"""Rule SQL rewriter -- read user SQL into the parts a hunt runs.

Takes a user-supplied SQL SELECT (raw, or the base query the HyperDX sanitizer
produced) and extracts:

1. The source table, from the top-level FROM
2. The detection logic: the PREWHERE and WHERE predicates, with time bounds
   removed so the hunt's own window applies
3. Whether the SELECT list has a bare ``*``

The SQL is read by ``hdx_sanitizer.split_time_window``, so a clause word inside
a string literal, a comment or a subquery never ends the WHERE early.

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

import re
from dataclasses import dataclass, field

from .hdx_sanitizer import HdxSanitizeError, split_time_window

TIME_PLACEHOLDER = "{timestamp_condition}"
"""Time-bound placeholder a rule stored before #683 may still carry; the hunt window replaces it."""

# The lookbehind starts the second form only at the first character of a whitespace
# run, so a long run is read once rather than once from each of its characters.
_TIME_PLACEHOLDER_CONJUNCT = re.compile(
    r"\{timestamp_condition\}\s+AND\s+|(?<!\s)\s+AND\s+\{timestamp_condition\}", re.IGNORECASE
)


def strip_time_placeholder(where: str) -> str:
    """``where`` without :data:`TIME_PLACEHOLDER`, which the worker never fills in.

    Left in, ClickHouse reads it as a query parameter with no type and refuses the
    statement. As a conjunct it is dropped; anywhere else it becomes ``1``, since the
    hunt window already bounds the scan in time.
    """
    return _TIME_PLACEHOLDER_CONJUNCT.sub("", where).replace(TIME_PLACEHOLDER, "1").strip()


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


@dataclass
class ParsedRule:
    """Result of parsing a user-supplied SQL statement."""

    source_db: str | None = None
    source_table: str | None = None
    where_clause: str = ""
    had_select_star: bool = False
    stripped_time_bounds: list[str] = field(default_factory=list)
    ignored_clauses: list[str] = field(default_factory=list)
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
        self._ts_columns = _TIMESTAMP_COLUMNS | (timestamp_columns or frozenset())

    def parse_user_sql(self, sql: str) -> ParsedRule:
        """Parse a user-supplied SQL SELECT statement.

        Extracts the source table, WHERE clause (with time bounds
        removed), and flags whether SELECT * was used.

        Args:
            sql: User-supplied SQL SELECT statement.

        Returns:
            ParsedRule with extracted components. SQL that cannot be read gives
            an empty ParsedRule carrying a warning, never an exception.
        """
        result = ParsedRule(original_sql=sql.strip())
        try:
            split = split_time_window(sql, self._ts_columns)
        except HdxSanitizeError as exc:
            result.warnings.append(f"Could not read the SQL: {exc}")
            return result

        result.source_db = split.source_db
        result.source_table = split.source_table
        result.had_select_star = split.select_star
        result.where_clause = strip_time_placeholder(split.filter or "")
        result.stripped_time_bounds = list(split.removed)
        result.ignored_clauses = list(split.ignored_clauses)

        if result.had_select_star:
            result.warnings.append(
                "SELECT * detected -- hunt output will use lean columns "
                "(matched_uuid + rule metadata + _json) instead."
            )
        if result.source_table is None:
            result.warnings.append("Could not extract source table from SQL.")
        if split.filter is None and not split.removed:
            result.warnings.append("No WHERE clause found in SQL.")
        for bound in split.stuck:
            result.warnings.append(
                f"A time bound sits under OR, NOT or a function call, so it stays in "
                f"the rule alongside the hunt window: {bound}"
            )
        if split.ignored_clauses:
            result.warnings.append(
                "A rule filters rows of one table, so it does not apply: "
                + ", ".join(split.ignored_clauses)
            )
        return result
