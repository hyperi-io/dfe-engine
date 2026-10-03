#  Project:      dfe-engine
#  File:         sampling/clickhouse_reader.py
#  Purpose:      Read sample _json lines from ClickHouse for the sampler
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse reads for the sampler.

All reads select ``toString(_json)`` as the first (and only) column so the
result is a list of raw event strings - the shape both the parsed-row path and
logreducer want. ``target`` arrives already quoted by the service (e.g.
``\\`db\\`.\\`events\\```); it is interpolated, not bound, because ClickHouse
cannot bind table names. ``filter`` must pass :func:`filter_predicate`, and what
is interpolated is the predicate rendered from its parse tree, not the caller's
text. Every read runs with :func:`read_settings`, so ClickHouse itself refuses
writes, DDL and settings changes.
"""

import re
from typing import Any

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

from dfe_engine.clickhouse.function_guard import refuse_calls_outside_the_row
from dfe_engine.clickhouse.quoting import column_reference
from dfe_engine.orgs.available_ids import ORG_ID_COLUMN

_CLICKHOUSE = Dialect.get_or_raise("clickhouse")

# ClickHouse reads a name standing alone in an IN list as a table, so the list may hold none.
_NAMES = (exp.Column, exp.Dot, exp.Identifier, exp.Var)

# Node types a predicate is built from: Conditions, and the parts sqlglot does not class as one.
_PREDICATE_PARTS = (
    exp.Condition,
    exp.DataType,
    exp.DataTypeParam,
    exp.Identifier,
    exp.Interval,
    exp.JSONPath,
    exp.JSONPathPart,
    exp.Lambda,
    exp.Tuple,
    exp.Var,
)

# No real column name holds these, and sqlglot renders a backslash in a quoted name unescaped.
_UNSAFE_NAME_CHARACTERS = frozenset('\\"`') | {chr(code) for code in range(0x20)} | {"\x7f"}

# sqlglot renders a JSON path key inside single quotes without escaping either of these.
_UNSAFE_PATH_CHARACTERS = frozenset("'\\")

# Escapes ClickHouse 26.9.4 decodes in a string that sqlglot keeps as written, changing the value.
_DIVERGENT_ESCAPES = frozenset({"e", "x", "N", '"', "/", "=", "`"})
_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


def filter_predicate(filter_sql: str) -> str | None:
    """Check a sampler filter is one condition over the sampled row, and render it.

    The filter must parse in the ClickHouse dialect as a single expression that
    reads only the sampled table's columns: no subquery or set operation, no
    statement separator or ``SETTINGS``/``FORMAT`` clause, no alias, no query
    parameter, no ``IN`` whose list holds a name or that names a table or
    function, no function form of ``IN`` (``globalIn``, ``notIn`` and the rest),
    no table function, no function that reads a dictionary, a Join table or a
    file, and no function that sends its arguments to another service (the
    ``ai*`` functions, ``generateSerialID``). The checks run on the caller's
    text and again on the parse of the rendered text, and the rendered text is
    what is returned, so the SQL that runs is the SQL that was checked.

    Args:
        filter_sql: The caller's filter.

    Returns:
        The condition rendered from its parse tree, or None for a blank filter.

    Raises:
        ValueError: If the filter is not one such condition. The message says why.
    """
    if not filter_sql.strip():
        return None
    tokens, statements = _parse(filter_sql)
    if any(token.token_type is TokenType.SEMICOLON for token in tokens):
        raise ValueError("filter must be one condition; ';' separates statements")
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("filter must be one condition")
    _refuse_divergent_escapes(filter_sql, tokens)
    _refuse_nodes(statements[0])

    rendered = statements[0].sql(dialect=_CLICKHOUSE, comments=False)
    tokens, reread = _parse(rendered)
    tree = reread[0] if len(reread) == 1 else None
    if tree is None or tree.sql(dialect=_CLICKHOUSE, comments=False) != rendered:
        raise ValueError("filter does not read back as the same condition once rendered")
    _refuse_nodes(tree)
    refuse_calls_outside_the_row(rendered, subject="filter", tokens=tokens)
    return rendered


def _parse(sql: str) -> tuple[list[Token], list[exp.Expr | None]]:
    """Tokenize and parse ``sql`` as ClickHouse, turning a parse failure into ValueError."""
    try:
        tokens = _CLICKHOUSE.tokenize(sql)
        statements = _CLICKHOUSE.parser().parse(tokens, sql)
    except SqlglotError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise ValueError(f"filter is not a ClickHouse condition: {detail}") from exc
    # sqlglot raises AttributeError and RecursionError on some input it cannot parse.
    except Exception as exc:
        raise ValueError("filter is not a ClickHouse condition sqlglot can parse") from exc
    return tokens, statements


def _refuse_divergent_escapes(sql: str, tokens: list[Token]) -> None:
    """Raise ValueError for a string escape whose rendering would change its value."""
    for token in tokens:
        if token.token_type is not TokenType.STRING:
            continue
        for match in _ESCAPE.finditer(sql, token.start, token.end + 1):
            if match.group(1) in _DIVERGENT_ESCAPES:
                raise ValueError(
                    f"filter may not use the escape \\{match.group(1)} in a string; "
                    "write the character itself"
                )


def _refuse_nodes(tree: exp.Expr) -> None:
    """Raise ValueError naming the first node in ``tree`` a sampler filter may not hold."""
    for node in tree.walk():
        refusal = _refusal(node)
        if refusal is not None:
            raise ValueError(refusal)


def _refusal(node: exp.Expr) -> str | None:
    """Why ``node`` may not appear in a sampler filter, or None when it may."""
    if isinstance(node, exp.Query):
        return "filter may not contain a subquery or set operation"
    if isinstance(node, exp.Alias):
        return (
            "filter may not contain an alias (AS), which can rename a column the read is bound on"
        )
    if isinstance(node, exp.In) and (
        any(node.args.get(arg) for arg in ("query", "field", "unnest")) or _lists_a_name(node)
    ):
        return "IN in a filter must list its values, not name a column, table or function"
    if isinstance(node, (exp.Placeholder, exp.Parameter)):
        return "filter may not contain query parameters"
    if isinstance(node, exp.Identifier) and _UNSAFE_NAME_CHARACTERS.intersection(node.name):
        return f"column name {node.name!r} may not contain a backslash, quote or control character"
    if isinstance(node, exp.JSONPathPart) and any(
        isinstance(value, str) and _UNSAFE_PATH_CHARACTERS.intersection(value)
        for value in node.args.values()
    ):
        return "a JSON path in a filter may not contain a quote or backslash"
    if not isinstance(node, _PREDICATE_PARTS):
        return f"filter may not contain {node.key.upper()}"
    return None


def _lists_a_name(node: exp.In) -> bool:
    """Whether any value in an IN list is, or holds, a name rather than only values."""
    for value in node.expressions:
        if any(isinstance(part, _NAMES) for part in value.walk()):
            return True
    return False


def read_settings(max_execution_time: int) -> dict[str, int]:
    """Settings for every sampler read: bounded in time, and read-only.

    The engine's ClickHouse user may write, so ``readonly=1`` makes ClickHouse
    refuse writes, DDL, reads through ``url()`` and any ``SETTINGS`` clause, as
    the raw-query adapter does.
    """
    return {"max_execution_time": max_execution_time, "readonly": 1}


def _raw_client(ch: Any) -> Any:
    """Unwrap the engine's ClickHouseClientWrapper to the clickhouse-connect Client.

    logreducer's ``ClickHouseSource`` wants the native driver client (block
    streaming); the wrapper stores it on ``_client``. A bare client passes through.
    """
    return getattr(ch, "_client", ch)


def column_names(ch: Any, target: str, max_execution_time: int) -> set[str]:
    """The column names of ``target``, an already-quoted table reference.

    Raises:
        clickhouse_connect.driver.exceptions.DatabaseError: If ClickHouse cannot
            describe the table, for one that does not exist among others.
    """
    result = ch.query(f"DESCRIBE TABLE {target}", settings=read_settings(max_execution_time))
    return {str(row[0]) for row in result.result_rows}


def build_where(
    *,
    source_label: str | None,
    org_ids: list[str] | None,
    filter_sql: str | None,
    since: str | None,
    until: str | None,
    timestamp_field: str,
) -> tuple[str, dict[str, Any]]:
    """Assemble a WHERE clause + bound parameters.

    ``source_label`` filters the shared landing table by ``_source``; leave it
    None when sampling a per-source table (already scoped). ``org_ids`` holds the
    read to rows whose ``_org_id`` is one of them, bound server-side; None reads
    every org. Time bounds bind server-side. ``filter_sql`` goes through
    :func:`filter_predicate`, and only the condition it renders is interpolated.

    Raises:
        ValueError: If ``filter_sql`` is not one condition over the sampled row, or
            ``org_ids`` is empty.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}
    timestamp = column_reference(timestamp_field)
    if org_ids is not None:
        if not org_ids:
            raise ValueError("a sample held to no org would read nothing")
        clauses.append(f"{ORG_ID_COLUMN} IN {{orgs:Array(String)}}")
        params["orgs"] = list(org_ids)
    if source_label:
        clauses.append("_source = {src:String}")
        params["src"] = source_label
    if since:
        clauses.append(f"{timestamp} >= {{since:DateTime64(3)}}")
        params["since"] = since
    if until:
        clauses.append(f"{timestamp} <= {{until:DateTime64(3)}}")
        params["until"] = until
    predicate = filter_predicate(filter_sql) if filter_sql else None
    if predicate:
        clauses.append(f"({predicate})")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def read_recent(
    ch: Any,
    target: str,
    *,
    limit: int,
    where: str,
    params: dict[str, Any],
    timestamp_field: str,
    max_execution_time: int,
) -> list[str]:
    """Newest ``limit`` ``_json`` rows, ordered by ``timestamp_field`` DESC."""
    sql = (
        f"SELECT toString(_json) FROM {target} {where} "  # noqa: S608 - quoted target; filter rendered by filter_predicate
        f"ORDER BY {column_reference(timestamp_field)} DESC LIMIT {{lim:UInt64}}"
    )
    return _run(ch, sql, {**params, "lim": limit}, max_execution_time)


def read_random(
    ch: Any,
    target: str,
    *,
    limit: int,
    where: str,
    params: dict[str, Any],
    seed: int | None,
    max_execution_time: int,
) -> list[str]:
    """Uniform-ish random ``limit`` rows via ``ORDER BY rand()``.

    A seed makes it reproducible. This is a full scan of the filtered set (CH has
    no cheap unseeded reservoir without a ``SAMPLE BY`` key), bounded by
    ``max_execution_time``; for very large tables prefer a tighter ``filter`` or
    time window.
    """
    rand = f"rand({seed})" if seed is not None else "rand()"
    sql = f"SELECT toString(_json) FROM {target} {where} ORDER BY {rand} LIMIT {{lim:UInt64}}"  # noqa: S608 - quoted target; filter rendered by filter_predicate
    return _run(ch, sql, {**params, "lim": limit}, max_execution_time)


def scan_query(
    target: str,
    *,
    where: str,
    scan_rows: int,
) -> str:
    """A stable, bounded SELECT for logreducer to reduce.

    ``ORDER BY cityHash64(_json)`` gives a deterministic pseudo-random subset so
    the reducer's multi-pass modes (dedup -> template -> anomaly) see the SAME
    rows on every pass, which its re-iterable-source contract requires. A bare
    ``LIMIT`` would return different rows per pass.
    """
    return (
        f"SELECT toString(_json) FROM {target} {where} "  # noqa: S608 - quoted target; filter rendered by filter_predicate
        f"ORDER BY cityHash64(toString(_json)) LIMIT {int(scan_rows)}"
    )


def _run(ch: Any, sql: str, params: dict[str, Any], max_execution_time: int) -> list[str]:
    result = ch.query(
        sql,
        parameters=params,
        settings=read_settings(max_execution_time),
    )
    return [r[0] for r in result.result_rows if r[0]]
