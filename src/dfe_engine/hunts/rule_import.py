#  Project:      dfe-engine
#  File:         hunts/rule_import.py
#  Purpose:      Read a SELECT into the fields a rule is saved with: its source and its WHERE
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Turn a pasted SELECT into a registered source and a WHERE clause, saving nothing.

A rule is a registered source plus the WHERE that selects its rows. A SELECT written
elsewhere carries more than that, so ``import_select`` reads the statement with sqlglot's
ClickHouse dialect, refuses the shapes a single-source row filter cannot express, and
lists everything it dropped. The WHERE text itself is sliced out of the statement by
``split_time_window`` and never regenerated from the parse tree, so the predicates that
come back are the ones the author wrote.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from dfe_engine.clickhouse.function_guard import CallNotPermittedError, tokenize
from dfe_engine.hunts.hdx_sanitizer import (
    REFUSED_CLAUSES,
    HdxSanitizeError,
    HdxSanitizeResult,
    split_time_window,
)
from dfe_engine.hunts.rule_rewriter import (
    TIME_PLACEHOLDER,
    TIMESTAMP_COLUMNS,
    strip_time_placeholder,
)

# Every quantifier below is possessive, so the patterns run in linear time on any input.
# The legacy export names the table through two placeholders and no database.
_TEMPLATE_FROM = re.compile(r"\{\{\s*+org_id\s*+\}\}\s*+\.\s*+\{\{\s*+source_table_name\s*+\}\}")
_TEMPLATE_TABLE = "dfe_import_template_source"
_TEMPLATE_REFERENCE = "{{org_id}}.{{source_table_name}}"
_DOUBLE_BRACE_TIME = re.compile(r"\{\{\s*+timestamp_condition\s*+\}\}")
# A bracketed placeholder is a conjunct of its own, so its AND goes with it.
_BRACKETED_TIME_CONJUNCT = re.compile(
    r"\(\s*+\{timestamp_condition\}\s*+\)\s++AND\s++"
    r"|(?<!\s)\s++AND\s++\(\s*+\{timestamp_condition\}\s*+\)",
    re.IGNORECASE,
)
# What a time placeholder leaves once its conjunct is gone: nothing that narrows.
_NO_FILTER = re.compile(r"\(*+\s*+1\s*+\)*+")

_GROUP_BY_REFUSAL = (
    "GROUP BY builds groups, and a rule matches single rows. Remove it, and the HAVING "
    "condition with it, or express the condition as a row filter."
)
_WITH_REFUSAL = "The query has a WITH clause. A rule reads one source table."
_FROM_SUBQUERY_REFUSAL = "The FROM clause is a subquery. A rule reads one source table."
_NOT_A_TABLE_REFUSAL = "The FROM clause is not a table name. A rule reads one source table."

# The Select clauses a rule does not keep, as the key sqlglot stores each under and its label.
_DROPPED_CLAUSES = (
    ("order", "ORDER BY"),
    ("limit", "LIMIT"),
    ("offset", "OFFSET"),
    ("settings", "SETTINGS"),
    ("format", "FORMAT"),
    ("distinct", "DISTINCT"),
)


class ImportRefusedError(ValueError):
    """The SELECT has a shape a rule cannot carry, and the message says which."""


@dataclass(frozen=True, slots=True)
class DroppedPart:
    """One piece of the SELECT the rule does not keep.

    Attributes:
        kind: ``columns``, ``order_by``, ``limit``, ``offset``, ``settings``, ``format``,
            ``distinct``, ``time_bound`` or ``time_placeholder``.
        text: The piece as written.
    """

    kind: str
    text: str


@dataclass(slots=True)
class RuleImport:
    """What a SELECT becomes as a rule's fields.

    Attributes:
        source: The registered source the FROM names, or None when the author must pick one.
        from_table: The FROM as written, or the template placeholders it was.
        where_clause: The row filter, time window removed, empty when nothing is left.
        dropped: Everything the rule does not keep.
        warnings: What the author should look at before saving.
    """

    source: str | None
    from_table: str
    where_clause: str
    dropped: list[DroppedPart] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_placeholders(where: str) -> list[str]:
    """The ``{name}`` and ``{name:Type}`` placeholders in a WHERE, outside string literals.

    A map literal such as ``{'k': 1}`` starts with a string and is not one.

    Args:
        where: A WHERE clause.

    Returns:
        Each placeholder's name in braces, in order. Empty when the text does not tokenize.
    """
    try:
        tokens = tokenize(where, subject="the WHERE clause")
    except CallNotPermittedError:
        return []
    found: list[str] = []
    for index, token in enumerate(tokens[:-2]):
        name, after = tokens[index + 1], tokens[index + 2]
        if (
            token.text == "{"
            and name.token_type is not TokenType.STRING
            and re.fullmatch(r"[A-Za-z_]\w*", name.text)
            and after.text in ("}", ":")
        ):
            found.append(f"{{{name.text}}}")
    return found


def _render(node: exp.Expr) -> str:
    return node.sql(dialect="clickhouse")


def _dropped_clauses(select: exp.Select) -> list[DroppedPart]:
    """The parts of the SELECT outside its FROM and WHERE that a rule leaves behind."""
    dropped: list[DroppedPart] = []
    columns = select.expressions
    if not (len(columns) == 1 and isinstance(columns[0], exp.Star)):
        dropped.append(DroppedPart("columns", ", ".join(_render(column) for column in columns)))
    for key, label in _DROPPED_CLAUSES:
        value = select.args.get(key)
        if not value:
            continue
        if isinstance(value, list):
            text = ", ".join(_render(item) for item in value)
        elif isinstance(value, exp.Expr):
            text = _render(value)
        else:
            text = label
        dropped.append(DroppedPart(label.lower().replace(" ", "_"), text))
    return dropped


def _one_select(statements: list[exp.Expr]) -> exp.Select:
    """The one SELECT, or a refusal naming the clause that makes it more than a row filter."""
    if len(statements) != 1:
        raise ImportRefusedError(f"Expected one SELECT statement, found {len(statements)}.")
    tree = statements[0]
    if isinstance(tree, exp.SetOperation):
        raise ImportRefusedError(REFUSED_CLAUSES[type(tree).__name__.upper()])
    if not isinstance(tree, exp.Select):
        raise ImportRefusedError("The statement is not a SELECT.")
    if tree.args.get("with_"):
        raise ImportRefusedError(_WITH_REFUSAL)
    if tree.args.get("joins"):
        raise ImportRefusedError(REFUSED_CLAUSES["JOIN"])
    if tree.args.get("group"):
        raise ImportRefusedError(_GROUP_BY_REFUSAL)
    for key, clause in (("having", "HAVING"), ("qualify", "QUALIFY"), ("windows", "WINDOW")):
        if tree.args.get(key):
            raise ImportRefusedError(REFUSED_CLAUSES[clause])
    return tree


def _from_table(select: exp.Select) -> exp.Table:
    """The FROM's table, or a refusal when it is a subquery or a table function."""
    source = select.args.get("from_")
    if source is None:
        raise ImportRefusedError("The query has no FROM clause.")
    table = source.this
    if isinstance(table, exp.Subquery):
        raise ImportRefusedError(_FROM_SUBQUERY_REFUSAL)
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        raise ImportRefusedError(_NOT_A_TABLE_REFUSAL)
    return table


def _resolve_source(
    table: exp.Table,
    templated: bool,
    is_source: Callable[[str], bool],
    data_database: str | None,
    warnings: list[str],
) -> tuple[str, str | None]:
    """The FROM as written, and the registered source it names, or None for the author to pick."""
    if templated:
        warnings.append("The FROM names its table through placeholders, so pick the source.")
        return _TEMPLATE_REFERENCE, None
    name, database = table.name, table.db
    written = f"{database}.{name}" if database else name
    if database and database != data_database:
        warnings.append(f"The FROM reads {written}, outside the data database, so pick the source.")
        return written, None
    if is_source(name):
        return written, name
    warnings.append(f"The FROM table {written} is not a registered source, so pick the source.")
    return written, None


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def import_select(
    sql: str,
    *,
    is_source: Callable[[str], bool],
    data_database: str | None,
) -> RuleImport:
    """Read a SELECT into a rule's source and WHERE, listing what the rule leaves out.

    Args:
        sql: One ClickHouse SELECT.
        is_source: Whether a label names a registered source.
        data_database: The database a source's table is read from. A FROM naming another
            database never resolves to a source.

    Returns:
        The source when the FROM names a registered one, else None with the table as written,
        the WHERE with the time window removed, and every part dropped.

    Raises:
        ImportRefusedError: The SQL is not one SELECT of a single table filtered by a WHERE.
    """
    text = _DOUBLE_BRACE_TIME.sub(TIME_PLACEHOLDER, sql.strip())
    if not text:
        raise ImportRefusedError("The SQL is empty.")
    dropped: list[DroppedPart] = []
    if TIME_PLACEHOLDER in text:
        dropped.append(DroppedPart("time_placeholder", TIME_PLACEHOLDER))
    templated = _TEMPLATE_FROM.search(text) is not None
    text = _BRACKETED_TIME_CONJUNCT.sub("", _TEMPLATE_FROM.sub(_TEMPLATE_TABLE, text))
    text = strip_time_placeholder(text)

    try:
        statements = [s for s in sqlglot.parse(text, read="clickhouse") if s is not None]
    except SqlglotError as exc:
        raise ImportRefusedError(
            f"The SQL does not parse as ClickHouse: {_first_line(exc)}"
        ) from exc
    select = _one_select(statements)
    table = _from_table(select)
    dropped = [*_dropped_clauses(select), *dropped]

    try:
        split = split_time_window(text, TIMESTAMP_COLUMNS)
    except HdxSanitizeError as exc:
        raise ImportRefusedError(f"The SQL could not be read: {exc}") from exc
    dropped.extend(DroppedPart("time_bound", bound) for bound in split.removed)
    warnings = [
        "A time bound sits under OR, NOT or a function call, so it stays in the filter "
        f"beside the hunt window: {bound}"
        for bound in split.stuck
    ]

    where = (split.filter or "").strip()
    if _NO_FILTER.fullmatch(where):
        where = ""
    if not where:
        warnings.append("No filter is left once the time window is removed. Write the WHERE.")
    placeholders = find_placeholders(where)
    if placeholders:
        warnings.append(
            f"The WHERE has {', '.join(placeholders)}, which the hunt runner never fills in. "
            "Replace it with a value or remove it."
        )

    from_table, source = _resolve_source(table, templated, is_source, data_database, warnings)
    return RuleImport(
        source=source,
        from_table=from_table,
        where_clause=where,
        dropped=dropped,
        warnings=warnings,
    )


def sanitize_summary(result: HdxSanitizeResult) -> dict[str, Any]:
    """What the HyperDX sanitiser removed, as the dict a response carries."""
    summary: dict[str, Any] = {}
    if result.stripped_time_bounds:
        summary["stripped_time_bounds"] = result.stripped_time_bounds
    if result.stripped_settings:
        summary["stripped_settings"] = result.stripped_settings
    if result.stripped_limit:
        summary["stripped_limit"] = result.stripped_limit
    if result.stripped_time_bucket_select:
        summary["stripped_time_bucket_select"] = result.stripped_time_bucket_select
    if result.stripped_time_bucket_refs:
        summary["stripped_time_bucket_refs"] = result.stripped_time_bucket_refs
    if result.had_time_bucket:
        summary["had_time_bucket"] = True
    if result.stripped_clauses:
        summary["stripped_clauses"] = result.stripped_clauses
    if result.warnings:
        summary["warnings"] = result.warnings
    return summary
