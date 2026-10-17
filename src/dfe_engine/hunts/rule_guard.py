#  Project:      dfe-engine
#  File:         rule_guard.py
#  Purpose:      Refuse a rule that matches every event, and band a rule's measured alert volume
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Guards a detection rule passes before it is saved.

``match_everything`` reads the rule's detection WHERE, with the time window
already taken out, and explains why it matches every event -- or returns None
when the condition narrows anything at all, or cannot be read.

``refuse_offbox_calls`` reads the same WHERE and refuses a condition that calls
a ClickHouse function reading outside the row or sending it to another service.
``refuse_condition_escape`` refuses one whose brackets do not balance or that reads
another table, since it runs inside a bracket beside the window and the hunt's
orgs, unless the table is in a lookup database the deployment lists as holding no
org's rows. ``refuse_rule_condition`` applies both, wherever a rule's condition is
saved or compiled.
``refuse_direct_query`` holds a hunt's hand-written ``query`` to that refusal and
to one statement that reads, or inserts what it reads.

``preview_sql`` counts what the rule matches over a lookback window on its own
source table, and ``volume_verdict`` turns that count into a band and the
plain-language warnings the UI shows beside the saved rule.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.simplify import simplify
from sqlglot.tokens import Token, TokenType

from ..clickhouse.function_guard import (
    CallNotPermittedError,
    refuse_calls_outside_the_row,
    tokenize,
)
from ..clickhouse.quoting import quote_identifier
from ..settings import LOOKUP_DATABASE_PATTERN, DetectionGuardSettings
from .hunt_output import matching_rows
from .rule_model import Rule

# Header columns every DFE event carries, so a presence check on one narrows nothing.
ALWAYS_PRESENT_COLUMNS = frozenset({"_json", "_uuid", "_timestamp", "_timestamp_load"})

# One-argument calls that keep a value present when their argument is present.
_PASS_THROUGH_CALLS = frozenset(
    {"tostring", "assumenotnull", "lower", "upper", "lowerutf8", "upperutf8"}
)
_PRESENT_CALLS = frozenset({"notempty", "isnotnull"})
_ABSENT_CALLS = frozenset({"empty", "isnull"})
_LENGTH_CALLS = frozenset({"length", "lengthutf8"})

_MINUTES_PER_DAY = 1440


class VolumeBand(StrEnum):
    """How much a rule's projected alert volume asks of the people reviewing it."""

    OK = "ok"
    GUIDANCE = "guidance"
    WARN = "warn"
    PLAINLY_BAD = "plainly_bad"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True, slots=True)
class VolumeMeasure:
    """What the preview counted over its lookback window.

    Attributes:
        window_minutes: Length of the lookback window.
        total: Events in the source table inside the window.
        matched: Of those, events the rule's condition matched.
    """

    window_minutes: int
    total: int
    matched: int

    @property
    def per_day(self) -> int:
        """Matches projected to a day at the window's rate."""
        return round(self.matched * _MINUTES_PER_DAY / self.window_minutes)

    @property
    def ratio(self) -> float | None:
        """Share of the window's events the rule matched, or None for an empty window."""
        return self.matched / self.total if self.total else None


def source_label(rule: Rule) -> str:
    """The ``<db>.<table>`` a rule scans, for messages."""
    if rule.source_db and rule.source_table:
        return f"{rule.source_db}.{rule.source_table}"
    return rule.source_table or rule.source or "its source"


# -- A rule that matches every event --------------------------------------


def match_everything(where: str, source: str) -> str | None:
    """Say why a detection condition matches every event, or None when it narrows.

    A condition matches everything when it reduces to TRUE: a tautology such as
    ``1=1``, a non-zero literal, or presence checks on header columns every event
    carries (``notEmpty(_json) = 1``, ``_json IS NOT NULL``). An empty condition
    is left to the rule's own execution check, which refuses it.

    Args:
        where: The rule's detection WHERE, time window already removed.
        source: The ``<db>.<table>`` the rule scans, for the message.

    Returns:
        The refusal message, or None when the condition narrows or cannot be read.
    """
    if not where.strip():
        return None
    try:
        tree = sqlglot.parse_one(where, read="clickhouse")
    except SqlglotError, RecursionError:
        return None

    present: list[str] = []

    def replace(node: exp.Expr) -> exp.Expr:
        column = _presence_check(node)
        if column is not None:
            if column not in present:
                present.append(column)
            return exp.true()
        if isinstance(node, (exp.And, exp.Or)):
            node.set("this", _as_condition(node.this))
            node.set("expression", _as_condition(node.expression))
        elif isinstance(node, exp.Not):
            node.set("this", _as_condition(node.this))
        return node

    try:
        reduced = simplify(_as_condition(tree.transform(replace)), dialect="clickhouse")
    except SqlglotError, RecursionError:
        return None
    if not (isinstance(reduced, exp.Boolean) and reduced.this is True):
        return None

    if present:
        names = " and ".join(present)
        verb, pronoun = ("is", "it") if len(present) == 1 else ("are", "them")
        why = (
            f"its condition is true whenever {names} {verb} present, and every event has {pronoun}"
        )
    else:
        why = "its condition is always true"
    return f"This rule matches every event in {source}: {why}. Add a condition that narrows it."


def _unwrap(node: exp.Expr) -> exp.Expr:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _call_name(node: exp.Expr) -> str | None:
    """Lower-cased name of a function call, or None for anything else."""
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return None


def _call_arg(node: exp.Expr) -> exp.Expr | None:
    """The only argument of a one-argument call, or None."""
    if isinstance(node, exp.Anonymous):
        return node.expressions[0] if len(node.expressions) == 1 else None
    arg = node.this
    return arg if isinstance(arg, exp.Expr) else None


def _header_column(node: exp.Expr) -> str | None:
    """The header column a value reads unchanged, through casts and ``toString``."""
    node = _unwrap(node)
    while True:
        arg = _call_arg(node) if _call_name(node) in _PASS_THROUGH_CALLS else None
        if isinstance(node, exp.Cast):
            node = _unwrap(node.this)
        elif arg is not None:
            node = _unwrap(arg)
        else:
            break
    if not isinstance(node, exp.Column):
        return None
    name = node.name.lower()
    # A header name under a header qualifier is a JSON path, such as _json._uuid.
    if name not in ALWAYS_PRESENT_COLUMNS or node.table.lower() in ALWAYS_PRESENT_COLUMNS:
        return None
    return name


def _number(node: exp.Expr) -> float | None:
    node = _unwrap(node)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.this)
        except ValueError:
            return None
    if isinstance(node, exp.Boolean):
        return 1.0 if node.this else 0.0
    return None


def _presence_check(node: exp.Expr) -> str | None:
    """The header column ``node`` only checks is present, or None."""
    node = _unwrap(node)
    arg = _call_arg(node) if _call_name(node) in _PRESENT_CALLS else None
    if arg is not None:
        return _header_column(arg)

    if isinstance(node, exp.Not):
        inner = _unwrap(node.this)
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return _header_column(inner.this)
        inner_arg = _call_arg(inner) if _call_name(inner) in _ABSENT_CALLS else None
        return _header_column(inner_arg) if inner_arg is not None else None

    if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE)):
        return None
    left, right = _unwrap(node.this), _unwrap(node.expression)
    if isinstance(node, exp.NEQ) and isinstance(right, exp.Literal):
        if right.is_string and right.this == "":
            return _header_column(left)

    value = _number(right)
    left_name = _call_name(left)
    left_arg = _call_arg(left) if left_name is not None else None
    if left_arg is None or value is None:
        return None
    # The comparison reads "the call is non-zero": = 1, != 0, > 0 or >= 1.
    nonzero = (
        (isinstance(node, exp.EQ) and value == 1)
        or (isinstance(node, (exp.NEQ, exp.GT)) and value == 0)
        or (isinstance(node, exp.GTE) and value == 1)
    )
    if left_name in _PRESENT_CALLS and nonzero:
        return _header_column(left_arg)
    if left_name in _LENGTH_CALLS and nonzero and not isinstance(node, exp.EQ):
        return _header_column(left_arg)
    if left_name in _ABSENT_CALLS and isinstance(node, exp.EQ) and value == 0:
        return _header_column(left_arg)
    return None


def _as_condition(node: exp.Expr) -> exp.Expr:
    """Read a bare number the way ClickHouse reads it as a condition: zero is false."""
    value = _number(node)
    if value is not None and not isinstance(_unwrap(node), exp.Boolean):
        return exp.true() if value != 0 else exp.false()
    return node


# -- Measured alert volume ------------------------------------------------

# The preview's two result columns, in the order the service reads them.
_TOTAL = "dfe_total"
_MATCHED = "dfe_matched"


def preview_sql(
    source_db: str, source_table: str, time_column: str, where: str, window_minutes: int
) -> str:
    """The count the volume preview runs: every event in the window, and the matches.

    The matches are counted over :func:`~dfe_engine.hunts.hunt_output.matching_rows`,
    the subquery the hunt runner's statements read, so neither count's alias is in
    scope for the rule and the preview matches the rows a run would write.

    Args:
        source_db: Database of the rule's source table.
        source_table: The rule's source table.
        time_column: The load-time column the hunt runner windows on.
        where: The rule's detection WHERE, time window already removed.
        window_minutes: Lookback window length.

    Returns:
        One SELECT returning ``dfe_total`` and ``dfe_matched``.
    """
    column = quote_identifier(time_column)
    window = f"{column} >= now() - INTERVAL {int(window_minutes)} MINUTE"
    source = f"{quote_identifier(source_db)}.{quote_identifier(source_table)}"
    matched = matching_rows(source_db, source_table, (column,), where, window)
    return (
        f"SELECT\n"  # noqa: S608 - quoted source and column; an integer window; the rule itself
        f"    (SELECT count() FROM {source} WHERE {window}) AS {_TOTAL},\n"
        f"    count() AS {_MATCHED}\n"
        f"FROM {matched}"
    )


def preview_settings(guard: DetectionGuardSettings) -> dict[str, float | int | str]:
    """Query settings that bound the preview and make it fail loudly at the bound.

    ``throw`` rather than ``break``: a broken-off aggregate returns zero rows,
    which would read as a rule that matches nothing.

    ``readonly=1`` because the preview runs the rule author's own condition: it
    makes ClickHouse refuse a read through ``url()`` whatever the engine's user
    is granted, as the sampler's reads do.
    """
    return {
        "max_execution_time": guard.preview_timeout_seconds,
        "timeout_overflow_mode": "throw",
        "max_rows_to_read": guard.preview_max_rows,
        "read_overflow_mode": "throw",
        "readonly": 1,
    }


def refuse_offbox_calls(where: str, *, subject: str = "A rule's detection condition") -> str | None:
    """Say why a detection condition may not run, or None when every call is fine.

    The condition is spliced into the preview's count and into the hunt
    runner's ``INSERT ... SELECT``, which needs write access and so cannot be
    made read-only. A call that reads outside the row or posts to another
    service is refused here instead -- in the API before the rule is saved, and
    again in the runner, which also compiles a rule YAML written straight into
    the deploy repo.

    Args:
        where: The rule's detection WHERE, as it will run.
        subject: What the message names the SQL as.

    Returns:
        The refusal message, or None when the condition calls nothing refused.
    """
    if not where.strip():
        return None
    try:
        refuse_calls_outside_the_row(where, subject=subject)
    except CallNotPermittedError as exc:
        return str(exc)
    return None


def refuse_condition_escape(
    where: str,
    *,
    subject: str = "A rule's detection condition",
    lookup_databases: Collection[str] = (),
) -> str | None:
    """Say why a condition would read rows past the bracket it runs in, or None when it cannot.

    The hunt runner wraps the condition in a bracket ANDed with the window and the
    hunt's orgs. A condition that closes it early, ``x = 1) OR (1 = 1``, would OR its
    way past both. One holding a SELECT, ``x IN (SELECT y FROM t)``, or naming a table
    after IN (:func:`_in_takes_a_table`) answers from rows neither holds, and its user
    is not pinned to an org. Brackets and keywords inside a string or a comment are
    not read.

    A database in ``lookup_databases`` carries no org's rows, so a rule may read one
    of its tables after IN: ``x IN db.t``, ``x IN (db.t)``, or
    ``x IN (SELECT <columns> FROM db.t [WHERE <condition>])`` (:func:`_lookup_refusal`).

    Args:
        where: The rule's detection WHERE, as it will run.
        subject: What the message names the SQL as.
        lookup_databases: ``hunts.lookup_databases``, the databases a lookup may read.

    Returns:
        The refusal message, or None when the condition stays inside its bracket.
    """
    try:
        tokens = tokenize(where, subject=subject)
    except CallNotPermittedError as exc:
        return str(exc)
    lookups = tuple(lookup_databases)
    for index, token in enumerate(tokens):
        if token.token_type is TokenType.SELECT:
            refusal = _lookup_refusal(where, tokens, index, lookups, subject)
            if refusal is not None:
                return refusal
    depth = 0
    for index, token in enumerate(tokens):
        if (
            token.token_type is TokenType.IN
            and not _in_takes_a_select(tokens, index)
            and _in_takes_a_table(tokens, index)
        ):
            table_db = _lookup_table_database(where, tokens, index)
            if table_db is None or table_db not in lookups:
                return (
                    f"{subject} may not name a table after IN, which reads its rows: "
                    f"use IN with a bracketed list of values{_lookup_hint(lookups)}"
                )
        if token.token_type is TokenType.L_PAREN:
            depth += 1
        elif token.token_type is TokenType.R_PAREN:
            depth -= 1
            if depth < 0:
                return f"{subject} closes a bracket it never opened"
    if depth:
        return f"{subject} leaves {depth} bracket(s) open"
    return None


def _lookup_hint(lookups: tuple[str, ...]) -> str:
    """The end of a refusal, naming what a lookup may read when any database is listed."""
    if not lookups:
        return ""
    return f", or a table of a lookup database ({', '.join(lookups)})"


# Clause words a lookup subquery may not carry, read only outside its brackets.
_LOOKUP_CLAUSE_WORDS = frozenset(
    {
        "ARRAY",
        "CROSS",
        "EXCEPT",
        "FINAL",
        "FORMAT",
        "FROM",
        "FULL",
        "GROUP",
        "HAVING",
        "INNER",
        "INTERSECT",
        "INTO",
        "JOIN",
        "LEFT",
        "LIMIT",
        "OFFSET",
        "ORDER",
        "PASTE",
        "PREWHERE",
        "QUALIFY",
        "RIGHT",
        "SAMPLE",
        "SELECT",
        "SETTINGS",
        "UNION",
        "WHERE",
        "WINDOW",
        "WITH",
    }
)

# Clause words that are also function names, so ``left(x, 3)`` is a call and not a join.
_CALLABLE_CLAUSE_WORDS = frozenset({"ARRAY", "FORMAT", "LEFT", "RIGHT"})


def _starts_a_clause(where: str, tokens: list[Token], position: int) -> bool:
    """Whether the token at ``position`` is a clause word, not a string, quoted name or call."""
    token = tokens[position]
    written = where[token.start : token.end + 1].split()
    word = written[0].upper() if written else ""
    if word not in _LOOKUP_CLAUSE_WORDS:
        return False
    following = tokens[position + 1 : position + 2]
    called = bool(following) and following[0].token_type is TokenType.L_PAREN
    return not (called and word in _CALLABLE_CLAUSE_WORDS)


_LOOKUP_NAME = re.compile(LOOKUP_DATABASE_PATTERN)

_QUOTES = ("`", '"')


def _plain_name(where: str, token: Token) -> str | None:
    r"""The name a token spells, when the SQL writes it plainly or plainly quoted, else None.

    An escape inside a quoted name, such as ``\x74``, is refused rather than read, so
    no reading of it can name a database other than the one compared.
    """
    raw = where[token.start : token.end + 1]
    quoted = {f"{quote}{token.text}{quote}" for quote in _QUOTES}
    if raw != token.text and raw not in quoted:
        return None
    return token.text if _LOOKUP_NAME.fullmatch(token.text) else None


def _qualified_table(where: str, tokens: list[Token], start: int) -> str | None:
    """The database of ``<db>.<table>`` written at ``start``, or None for any other shape."""
    parts = tokens[start : start + 3]
    if len(parts) < 3 or parts[1].token_type is not TokenType.DOT:
        return None
    database, table = _plain_name(where, parts[0]), _plain_name(where, parts[2])
    if database is None or table is None:
        return None
    following = tokens[start + 3 : start + 4]
    if following and following[0].token_type in (TokenType.DOT, TokenType.L_PAREN):
        return None
    return database


def _in_takes_a_select(tokens: list[Token], index: int) -> bool:
    """Whether the IN at ``index`` takes ``(SELECT``, which :func:`_lookup_refusal` judges."""
    following = tokens[index + 1 : index + 3]
    return (
        len(following) == 2
        and following[0].token_type is TokenType.L_PAREN
        and following[1].token_type is TokenType.SELECT
    )


def _lookup_table_database(where: str, tokens: list[Token], index: int) -> str | None:
    """The database the IN at ``index`` reads a table of, as ``IN db.t`` or ``IN ((db.t))``.

    None when what follows IN is not one ``<db>.<table>`` inside as many closing
    brackets as it opened.
    """
    opened = index + 1
    while opened < len(tokens) and tokens[opened].token_type is TokenType.L_PAREN:
        opened += 1
    brackets = opened - index - 1
    database = _qualified_table(where, tokens, opened)
    closing = tokens[opened + 3 : opened + 3 + brackets]
    if database is None or len(closing) < brackets:
        return None
    if any(token.token_type is not TokenType.R_PAREN for token in closing):
        return None
    return database


# The one subquery a lookup may be, as a refusal names it.
_LOOKUP_SHAPE = "IN (SELECT <columns> FROM <db>.<table> [WHERE <condition>])"


def _closing_bracket(tokens: list[Token], opened: int) -> int | None:
    """The index of the bracket closing the one at ``opened``, or None when none does."""
    depth = 0
    for index in range(opened, len(tokens)):
        if tokens[index].token_type is TokenType.L_PAREN:
            depth += 1
        elif tokens[index].token_type is TokenType.R_PAREN:
            depth -= 1
            if depth == 0:
                return index
    return None


def _lookup_refusal(
    where: str, tokens: list[Token], index: int, lookups: tuple[str, ...], subject: str
) -> str | None:
    """Why the SELECT at ``index`` may not run, or None for a lookup in a listed database.

    A lookup is ``IN (SELECT <columns> FROM <db>.<table> [WHERE <condition>])`` with
    ``<db>`` listed. Anything else after its FROM, a second FROM or any clause word
    outside its own brackets could read another table, so it is refused. A SELECT
    nested inside it is judged on its own, and so is an IN naming a table.
    """
    if not lookups:
        return f"{subject} may not hold a SELECT, which reads rows outside the hunt's orgs"
    shape = (
        f"{subject} may hold a SELECT only as {_LOOKUP_SHAPE} "
        f"on a lookup database ({', '.join(lookups)})"
    )
    opened = index - 1
    if opened < 1 or tokens[opened].token_type is not TokenType.L_PAREN:
        return shape
    if tokens[opened - 1].token_type is not TokenType.IN:
        return shape
    closed = _closing_bracket(tokens, opened)
    if closed is None:
        return f"{subject} leaves a SELECT's bracket open"
    body = tokens[index + 1 : closed]
    depth = 0
    clauses: list[int] = []
    for position, token in enumerate(body):
        if token.token_type is TokenType.L_PAREN:
            depth += 1
        elif token.token_type is TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and _starts_a_clause(where, body, position):
            clauses.append(position)
    if not clauses or body[clauses[0]].token_type is not TokenType.FROM or clauses[0] == 0:
        return shape
    database = _qualified_table(where, body, clauses[0] + 1)
    if database is None:
        return shape
    if database not in lookups:
        return (
            f"{subject} may not read database {database}, which is not a lookup database "
            f"({', '.join(lookups)})"
        )
    after_table = clauses[0] + 4
    if len(clauses) == 1 and after_table == len(body):
        return None
    filtered = len(clauses) == 2 and clauses[1] == after_table
    if filtered and body[after_table].token_type is TokenType.WHERE:
        return None
    return shape


_NAME_TOKENS = frozenset({TokenType.VAR, TokenType.IDENTIFIER, TokenType.DOT})
_LITERAL_TOKENS = frozenset({TokenType.STRING, TokenType.NUMBER})


def _names_something(token: Token) -> bool:
    """Whether a token can be part of a table name: a word, a quoted name or a dot."""
    if token.token_type in _NAME_TOKENS:
        return True
    return token.token_type not in _LITERAL_TOKENS and token.text.isidentifier()


def _in_takes_a_table(tokens: list[Token], index: int) -> bool:
    """Whether the IN at ``index`` takes a table: ``IN t``, ``IN db.t``, ``IN (t)``, ``IN ((t))``.

    ClickHouse reads a lone name after IN, bracketed or not, as a table unless a
    column of that name shadows it. Anything but a bracket after IN is refused, and
    a bracket holding nothing but a name is too.
    """
    following = tokens[index + 1 :]
    opened = 0
    while opened < len(following) and following[opened].token_type is TokenType.L_PAREN:
        opened += 1
    if not opened:
        return True
    inside = following[opened:]
    run = 0
    while run < len(inside) and _names_something(inside[run]):
        run += 1
    return 0 < run < len(inside) and inside[run].token_type is TokenType.R_PAREN


def refuse_rule_condition(
    where: str,
    *,
    subject: str = "A rule's detection condition",
    lookup_databases: Collection[str] = (),
) -> str | None:
    """Say why a rule's condition may not run, or None when it may.

    :func:`refuse_offbox_calls`, then :func:`refuse_condition_escape`: the API, the
    governance actions and the hunt runner refuse the same conditions.

    Args:
        where: The rule's detection WHERE, as it will run.
        subject: What the message names the SQL as.
        lookup_databases: ``hunts.lookup_databases``, the databases a lookup may read.

    Returns:
        The first refusal message, or None.
    """
    if not where.strip():
        return None
    return refuse_offbox_calls(where, subject=subject) or refuse_condition_escape(
        where, subject=subject, lookup_databases=lookup_databases
    )


_QUERY_SUBJECT = "A hunt's query"


def refuse_direct_query(query: str) -> str | None:
    """Say why a hunt's direct ``query`` may not run, or None when it may.

    The hunt runner sends the query to ClickHouse as written, as a user that may
    write, so nothing but the statement's shape stops a ``DROP`` or an ``ALTER ...
    DELETE``. It must be one SELECT (``WITH`` and ``UNION`` included), or one
    ``INSERT INTO`` a table from a SELECT, which is how a hunt writes its
    detections, and it may call nothing :func:`refuse_offbox_calls` refuses.

    Args:
        query: The hunt's ``query``, as the runner will send it.

    Returns:
        The refusal message, starting ``A hunt's query``, or None.
    """
    offbox = refuse_offbox_calls(query, subject=_QUERY_SUBJECT)
    if offbox is not None:
        return offbox
    try:
        statements = [s for s in sqlglot.parse(query, read="clickhouse") if s is not None]
    except (SqlglotError, RecursionError) as exc:
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return f"{_QUERY_SUBJECT} does not parse as ClickHouse SQL: {detail}"
    if len(statements) != 1:
        return f"{_QUERY_SUBJECT} must be one statement, not {len(statements)}"
    statement = statements[0]
    if isinstance(statement, exp.Query) or _inserts_a_select(statement):
        return None
    keyword = statement.sql(dialect="clickhouse").split(None, 1)[0].upper()
    return (
        f"{_QUERY_SUBJECT} must be a SELECT, or an INSERT INTO a table from a SELECT, not {keyword}"
    )


def _inserts_a_select(statement: exp.Expr) -> bool:
    """Whether a statement is ``INSERT INTO <table> [(columns)] <SELECT>``."""
    if not isinstance(statement, exp.Insert) or not isinstance(statement.expression, exp.Query):
        return False
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    return isinstance(target, exp.Table)


def volume_verdict(
    measure: VolumeMeasure, guard: DetectionGuardSettings, source: str
) -> tuple[VolumeBand, list[str]]:
    """Band a measured match count and word the warnings for it.

    Args:
        measure: What the preview counted.
        guard: The deployment's thresholds.
        source: The ``<db>.<table>`` the rule scans, for the messages.

    Returns:
        The band, and the warnings to show beside the rule (none when it is OK).
    """
    window = measure.window_minutes
    if measure.total == 0:
        return VolumeBand.UNMEASURED, [
            f"No events reached {source} in the last {window} minutes, so this rule's "
            "alert volume could not be measured."
        ]

    per_day = measure.per_day
    ratio = measure.ratio if measure.total >= guard.min_rows_for_ratio else None
    lead = (
        f"Over the last {window} minutes this rule matched {measure.matched:,} of "
        f"{measure.total:,} events in {source}, about {per_day:,} alerts a day"
    )
    broad = ratio is not None and ratio >= guard.block_match_ratio
    if per_day > guard.block_per_day or (broad and per_day > guard.ack_per_day):
        band = VolumeBand.PLAINLY_BAD
        warnings = [f"{lead} -- far more than a team can review. Narrow it before it runs."]
    elif per_day > guard.ack_per_day:
        band = VolumeBand.WARN
        warnings = [f"{lead} -- consider narrowing it."]
    elif per_day > guard.warn_per_day:
        band = VolumeBand.GUIDANCE
        warnings = [f"{lead} -- more than one analyst typically reviews."]
    else:
        band = VolumeBand.OK
        warnings = []

    if ratio is not None and ratio >= guard.warn_match_ratio:
        warnings.append(
            f"That is {ratio:.0%} of all events in {source}. Check the condition "
            "narrows it to what you meant."
        )
        if band is VolumeBand.OK:
            band = VolumeBand.GUIDANCE
    return band, warnings


def unmeasured_warning(reason: str) -> str:
    """The warning for a preview that could not finish, naming why."""
    return (
        f"The alert-volume preview could not finish: {reason}. This rule's alert "
        "volume is unknown until it runs."
    )
