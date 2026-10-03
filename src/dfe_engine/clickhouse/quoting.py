#  Project:      dfe-engine
#  File:         clickhouse/quoting.py
#  Purpose:      Canonical ClickHouse identifier + literal quoting (injection-safe)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Canonical ClickHouse identifier + literal quoting - ONE injection-safe seam.

Every DDL / SQL builder quotes through here instead of re-deriving the rules (the
CH-sprawl audit found four independent, inconsistent escapers). PREFER server-side
parameter binding (clickhouse-connect ``parameters={name: Type}``) for VALUES on
the read path; use :func:`quote_literal` only where a value must be spliced into
DDL/DML text that has no bind slot (row-policy predicates, meta-table projections).
"""

import re

# A name a query may carry bare: one identifier, or a dotted path of them. Use fullmatch.
BARE_REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

# A source table takes its source's label as its name, and a label joins its words with '-'.
_SOURCE_PART = r"[A-Za-z_][A-Za-z0-9_-]*"
_SOURCE_NAME = re.compile(rf"{_SOURCE_PART}(?:\.{_SOURCE_PART})?")

# One part of a table reference: bare, or backtick-quoted with no backtick or backslash inside.
_TABLE_PART = r"(?:[A-Za-z_][A-Za-z0-9_]*|`[^`\\]+`)"
_TABLE_REFERENCE = re.compile(rf"({_TABLE_PART})(?:\.({_TABLE_PART}))?")

# Names ClickHouse reads as a literal or a clause word, not a column, when bare in
# a WHERE, ORDER BY, GROUP BY or SELECT list (measured on ClickHouse 26.9).
_NOT_A_COLUMN_WHEN_BARE = frozenset(
    {
        "all",
        "cube",
        "distinct",
        "false",
        "inf",
        "infinity",
        "nan",
        "not",
        "null",
        "rollup",
        "top",
        "true",
    }
)


def quote_identifier(name: str) -> str:
    """Backtick-quote a CH identifier - escape backslashes THEN backticks.

    For a SINGLE name (org, role, column). A dotted ``db.table`` reference must
    quote each part separately - do not pass it whole (it would become one
    identifier literally named ``db.table``).

    ClickHouse honours C-style backslash escapes inside a backtick-quoted
    identifier as it does in a string literal, so ``\\`` is doubled first, for the
    reason :func:`quote_literal` gives: a lone one is read as an escape, which
    renders the wrong name or consumes the closing backtick.
    """
    return "`" + name.replace("\\", "\\\\").replace("`", "``") + "`"


def quote_literal(value: str) -> str:
    """Single-quote a CH string literal - escape backslashes THEN single quotes.

    Order is load-bearing: ClickHouse honours C-style backslash escapes, so ``\\``
    MUST be doubled before ``'`` is doubled, else a crafted ``\\' OR 1=1`` breaks
    out of a RESTRICTIVE row-policy predicate and tenant isolation fails open
    (F-ROWPOLICY-BACKSLASH, proven live on CH 25.8). Matches clickhouse-connect's
    ``escape_str``. Prefer server-side ``parameters={}`` binding where a slot exists.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def table_reference(reference: str) -> str:
    """Render a caller-supplied ``table`` or ``db.table`` as a quoted table reference.

    Each part may arrive bare or backtick-quoted. Both are re-quoted through
    :func:`quote_identifier`, so the result names a table and nothing else.

    Args:
        reference: ``table``, ``db.table``, or either with backtick-quoted parts.

    Returns:
        The reference with every part backtick-quoted.

    Raises:
        ValueError: If the reference is not one table name, optionally database-qualified.
    """
    match = _TABLE_REFERENCE.fullmatch(reference.strip())
    if match is None:
        raise ValueError(f"not a table reference: {reference!r}")
    parts = [part.strip("`") for part in match.groups() if part is not None]
    return ".".join(quote_identifier(part) for part in parts)


def plain_table_name(reference: str) -> tuple[str, str]:
    """Split a caller-supplied ``table`` or ``database.table`` written in identifier characters.

    For a name that is configuration text rather than a ClickHouse reference, so
    nothing but letters, digits and ``_`` may reach the statement. Quote both
    parts through :func:`quote_identifier` wherever the name is spliced.

    Args:
        reference: The name as written, surrounding whitespace ignored.

    Returns:
        ``(database, table)``, with ``database`` empty for an unqualified name.

    Raises:
        ValueError: If the name is anything but one identifier, or two joined by
            a single dot.
    """
    name = reference.strip()
    if not BARE_REFERENCE.fullmatch(name) or name.count(".") > 1:
        raise ValueError(
            f"{reference!r} is not a table name: use table or database.table, "
            "each made of letters, digits and '_'"
        )
    database, _, table = name.rpartition(".")
    return database, table


def plain_source_name(reference: str) -> tuple[str, str]:
    """Split a caller-supplied source ``table`` or ``database.table``, ``-`` allowed in each part.

    A source table is named for its source label, a DNS-1123 label such as
    ``cisco-ios``. The ``-`` is safe only because both parts reach the statement
    through :func:`quote_identifier`. A results table is still held to
    :func:`plain_table_name`.

    Args:
        reference: The name as written, surrounding whitespace ignored.

    Returns:
        ``(database, table)``, with ``database`` empty for an unqualified name.

    Raises:
        ValueError: If the name is anything but one name, or two joined by a
            single dot, each starting with a letter or ``_``.
    """
    name = reference.strip()
    if not _SOURCE_NAME.fullmatch(name):
        raise ValueError(
            f"{reference!r} is not a table name: use table or database.table, "
            "each made of letters, digits, '_' and '-'"
        )
    database, _, table = name.rpartition(".")
    return database, table


def column_reference(name: str) -> str:
    """Render an operator-supplied field name as a column reference.

    A plain identifier or dotted path is returned as written, so a nested JSON
    path keeps resolving. Any other name, and any segment ClickHouse would read as
    a literal or clause word (``null``, ``not``, ``true``, ...), is quoted one
    dotted segment at a time through :func:`quote_identifier`, which ClickHouse
    resolves the same way, so no field name can carry SQL into the query.

    Args:
        name: The bare field name, after any mapping the caller applies.

    Returns:
        The column reference to splice into the query.

    Raises:
        ValueError: If the name is already wrapped in backticks. Quoting it again
            would name a different column, one whose name holds the backticks.
    """
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        raise ValueError(f"pass the bare field name, not a quoted one: {name!r}")
    parts = name.split(".")
    if BARE_REFERENCE.fullmatch(name) and not any(
        part.lower() in _NOT_A_COLUMN_WHEN_BARE for part in parts
    ):
        return name
    return ".".join(quote_identifier(part) for part in parts)
