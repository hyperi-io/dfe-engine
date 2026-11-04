#  Project:      dfe-engine
#  File:         schema/applier.py
#  Purpose:      Bring a live ClickHouse to the schema's DDL, additively, and report
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bring a live ClickHouse to the schema's DDL, and say exactly what changed.

Every table converges from its CREATE TABLE statement, whoever rendered it:
dfe-schemas for a manifest object, the DDL generator for a source's landing
table. The statement is read with ClickHouse's own tokens and compared with
``system.*``. An absent table is created. A present one gains the columns it
lacks, with their indexes, and is brought to the JSON settings its CREATE
declares for this platform, so a deployment moved between ClickHouse Cloud and
self-hosted converges.

A source table also moves its TTL to the one its version declares and brings
the indexes of the columns it keeps to theirs. A manifest table refuses those
instead: a changed column type or codec, ORDER BY, PARTITION BY or TTL is named
and left, because each rewrites or expires data, and ``dfe schema apply
--allow-drift`` carries out the two ClickHouse has an operation for. A table
whose TTL follows the deployment default takes a change of that default through
:meth:`ManifestApplier.reconcile_ttl`.

Every statement renders through dfe-schemas: the database replicates its own
DDL, every table takes the argumentless ``Replicated`` engine, and nothing
carries ``ON CLUSTER``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from dfe_schemas.clickhouse import (
    Platform,
    create_database,
    engine_clause,
    parse_engine,
    platform_only_setting_names,
    realign_settings,
)
from dfe_schemas.deploy_defaults import JSON_TABLE_SETTINGS
from dfe_schemas.render import RenderedObject
from scalo.logger import logger
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType

from dfe_engine import __version__ as engine_version
from dfe_engine.clickhouse.statements import StatementTooLargeError, ddl_settings
from dfe_engine.schema.ledger import LedgerRow, MigrationLedger
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator, quote_ident
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

Action = Literal["created", "altered", "unchanged", "refused", "skipped"]

_CLICKHOUSE = Dialect.get_or_raise("clickhouse")

# The clauses after a CREATE TABLE body, as engine_full also prints them.
_CLAUSES = ("PARTITION BY", "PRIMARY KEY", "ORDER BY", "SAMPLE", "TTL", "SETTINGS", "COMMENT")
_COLUMN_CLAUSES = frozenset({"DEFAULT", "MATERIALIZED", "ALIAS", "EPHEMERAL", "COMMENT", "CODEC"})
_OPEN = frozenset({TokenType.L_PAREN, TokenType.L_BRACKET, TokenType.L_BRACE})
_CLOSE = frozenset({TokenType.R_PAREN, TokenType.R_BRACKET, TokenType.R_BRACE})
_NAMES = frozenset({TokenType.IDENTIFIER, TokenType.STRING})
# After one of these an operand follows, so a clause word there is a bare column name.
_OPERAND_NEXT = frozenset(
    {
        *_OPEN,
        TokenType.COMMA,
        TokenType.PLUS,
        TokenType.DASH,
        TokenType.STAR,
        TokenType.SLASH,
        TokenType.MOD,
        TokenType.EQ,
        TokenType.NEQ,
        TokenType.GT,
        TokenType.GTE,
        TokenType.LT,
        TokenType.LTE,
        TokenType.AND,
        TokenType.OR,
        TokenType.NOT,
        TokenType.WHERE,
        TokenType.INTERVAL,
    }
)

# Seconds in each unit of fixed length. A month, quarter or year depends on the calendar.
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3_600, "day": 86_400, "week": 604_800}
_DAY_SECONDS = 86_400

# What the one DDL shape and Cloud's rewrite put before a variant. Config never declares them.
_ENGINE_PREFIXES = ("Replicated", "Shared")


class SchemaApplyError(Exception):
    """A DDL statement failed, or ClickHouse or a DDL text could not be read.

    Raised rather than logged: the standalone entry point is a GATE, and a gate
    that reports success on a failed apply lets the data plane start against a
    database that has no tables.
    """


# -- reading DDL text ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveTtl:
    """The interval of a table's TTL, as ClickHouse prints it.

    Attributes:
        amount: The interval's count, 0 when no interval could be read from the clause.
        unit: The interval's unit in lower case, such as ``day`` or ``month``, empty
            when no interval could be read from the clause.
    """

    amount: int
    unit: str

    @property
    def days(self) -> int | None:
        """The interval in whole days, or None when it is not a whole number of days."""
        seconds = _UNIT_SECONDS.get(self.unit)
        if seconds is None or (self.amount * seconds) % _DAY_SECONDS:
            return None
        return self.amount * seconds // _DAY_SECONDS

    def describe(self) -> str:
        """Whole days as a number (``90``), else the interval as printed (``6 hour``)."""
        if self.days is not None:
            return str(self.days)
        return f"{self.amount} {self.unit}" if self.unit else "unknown"


@dataclass(frozen=True, slots=True)
class IndexDefinition:
    """One data-skipping index as a CREATE declares it.

    Attributes:
        name: The index name.
        type: Its ``TYPE`` clause, as written.
        granularity: Its ``GRANULARITY``, None where the server picks it.
        sql: The whole ``INDEX ...`` definition.
        columns: The token texts of its expression, which include the columns it reads.
    """

    name: str
    type: str
    granularity: int | None
    sql: str
    columns: frozenset[str]

    def matches(self, live_type: str, live_granularity: int) -> bool:
        """Whether a live index of that type and granularity is this one."""
        return _squash(live_type).lower() == _squash(self.type).lower() and (
            self.granularity is None or live_granularity == self.granularity
        )


@dataclass(frozen=True, slots=True)
class TableDefinition:
    """What a CREATE TABLE declares, in the text it declares it in.

    Attributes:
        columns: Column name to everything its declaration carries after the name.
        types: Column name to its type alone.
        codecs: Column name to its codec families, for the columns that pin one.
        indexes: Every data-skipping index.
        clauses: ``ORDER BY``, ``TTL`` and the other table clauses, keyword to text.
        settings: The SETTINGS clause, name to the value as written.
        ttl: The TTL clause's interval, None when the table declares no TTL.
    """

    columns: dict[str, str]
    types: dict[str, str]
    codecs: dict[str, tuple[str, ...]]
    indexes: tuple[IndexDefinition, ...]
    clauses: dict[str, str]
    settings: dict[str, str]
    ttl: LiveTtl | None

    @property
    def json_settings(self) -> dict[str, int]:
        """The JSON table settings it declares, the only settings moved on a live table."""
        managed = set(JSON_TABLE_SETTINGS) | platform_only_setting_names()
        return {
            name: int(value)
            for name, value in self.settings.items()
            if name in managed and value.isdigit()
        }


def _squash(text: str) -> str:
    """*text* with its whitespace out, since a type or key carries no meaningful space."""
    return "".join(text.split())


def _tokens(text: str) -> list[Token]:
    """ClickHouse's tokens for *text*, up to the first statement end."""
    try:
        tokens = _CLICKHOUSE.tokenize(text)
    # sqlglot raises AttributeError and RecursionError on some input it cannot read.
    except (SqlglotError, AttributeError, RecursionError) as exc:
        raise SchemaApplyError(f"could not read DDL: {exc}") from exc
    end = next((i for i, t in enumerate(tokens) if t.token_type is TokenType.SEMICOLON), None)
    return tokens[:end]


def _depth(token: Token) -> int:
    return (token.token_type in _OPEN) - (token.token_type in _CLOSE)


def _split(tokens: Sequence[Token]) -> list[list[Token]]:
    """*tokens* cut at the commas outside any bracket."""
    parts: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.token_type is TokenType.COMMA and depth == 0:
            parts.append([])
            continue
        depth += _depth(token)
        parts[-1].append(token)
    return [part for part in parts if part]


def _closing(tokens: Sequence[Token], start: int) -> int:
    """The index of the bracket that closes the one at *start*."""
    depth = 0
    for end in range(start, len(tokens)):
        depth += _depth(tokens[end])
        if depth == 0:
            return end
    return len(tokens) - 1


def _span(text: str, tokens: Sequence[Token]) -> str:
    return text[tokens[0].start : tokens[-1].end + 1] if tokens else ""


def _word(token: Token) -> str:
    """A keyword's spelling. Quoted names and strings never read as one."""
    return "" if token.token_type in _NAMES else token.text.upper()


def _top(tokens: Sequence[Token]) -> list[tuple[int, Token]]:
    """Each token outside any bracket, with its index."""
    top: list[tuple[int, Token]] = []
    depth = 0
    for i, token in enumerate(tokens):
        if depth == 0:
            top.append((i, token))
        depth += _depth(token)
    return top


def _clauses(tokens: Sequence[Token]) -> dict[str, list[Token]]:
    """The table clauses outside any bracket, keyword to the tokens after it.

    A clause word that opens a clause or follows an operator is a column name, as
    in ``TTL ttl + INTERVAL 1 DAY``.
    """
    clauses: dict[str, list[Token]] = {}
    current: list[Token] | None = None
    depth = 0
    for i, token in enumerate(tokens):
        opens = current is None or (current and tokens[i - 1].token_type not in _OPERAND_NEXT)
        if depth == 0 and _word(token) in _CLAUSES and opens:
            current = clauses.setdefault(_word(token), [])
            continue
        depth += _depth(token)
        if current is not None:
            current.append(token)
    return clauses


def _interval(tokens: Sequence[Token]) -> LiveTtl:
    """The first ``INTERVAL N UNIT`` or ``toInterval<Unit>(N)`` in *tokens*."""
    for i, token in enumerate(tokens[:-2]):
        after = tokens[i + 1 : i + 3]
        if _word(token) == "INTERVAL" and after[0].token_type is TokenType.NUMBER:
            return LiveTtl(amount=int(after[0].text), unit=after[1].text.lower())
        unit = token.text.removeprefix("toInterval")
        if unit and unit != token.text and after[1].token_type is TokenType.NUMBER:
            return LiveTtl(amount=int(after[1].text), unit=unit.lower())
    return LiveTtl(amount=0, unit="")


def _settings(text: str, tokens: Sequence[Token]) -> dict[str, str]:
    """A SETTINGS clause, name to the value as written."""
    return {
        part[0].text: _span(text, part[2:])
        for part in _split(tokens)
        if len(part) > 2 and part[1].token_type is TokenType.EQ
    }


def _codecs(tokens: Sequence[Token]) -> tuple[str, ...]:
    """The codec FAMILIES of a ``CODEC(...)``, arguments dropped.

    The server reports a codec with its resolved arguments, ``Delta(8)`` for a
    declared ``Delta``, so only the families compare.
    """
    start = next((i for i, t in _top(tokens) if _word(t) == "CODEC"), None)
    if start is None or start + 1 >= len(tokens):
        return ()
    end = _closing(tokens, start + 1)
    return tuple(_word(part[0]) for part in _split(tokens[start + 2 : end]))


def _index(text: str, part: Sequence[Token]) -> IndexDefinition:
    """``INDEX name expr TYPE type [GRANULARITY n]``."""
    keyword = {_word(t): i for i, t in _top(part) if _word(t) in ("TYPE", "GRANULARITY")}
    type_end = keyword.get("GRANULARITY", len(part))
    index_type = _span(text, part[keyword["TYPE"] + 1 : type_end])
    granularity = part[type_end + 1].text if type_end + 1 < len(part) else ""
    # ClickHouse stores its own granularity for a text index, whatever the DDL said.
    server_owned = _squash(index_type).lower().startswith("text")
    return IndexDefinition(
        name=part[1].text,
        type=index_type,
        granularity=None if server_owned or not granularity else int(granularity),
        sql=_span(text, part),
        columns=frozenset(t.text for t in part[2 : keyword["TYPE"]]),
    )


def read_table(statement: str) -> TableDefinition:
    """What *statement*, one CREATE TABLE, declares.

    Raises:
        SchemaApplyError: The statement could not be tokenised, or has no column body.
    """
    tokens = _tokens(statement)
    start = next((i for i, t in enumerate(tokens) if t.token_type is TokenType.L_PAREN), None)
    if start is None:
        raise SchemaApplyError(f"not a CREATE TABLE with columns: {statement[:80]!r}")
    end = _closing(tokens, start)
    columns: dict[str, str] = {}
    types: dict[str, str] = {}
    codecs: dict[str, tuple[str, ...]] = {}
    indexes: list[IndexDefinition] = []
    for part in _split(tokens[start + 1 : end]):
        head = _word(part[0])
        if head == "INDEX":
            indexes.append(_index(statement, part))
            continue
        if head in ("PROJECTION", "CONSTRAINT") or len(part) < 2:
            continue
        name, rest = part[0].text, part[1:]
        columns[name] = _span(statement, rest)
        type_end = next((i for i, t in _top(rest) if _word(t) in _COLUMN_CLAUSES), len(rest))
        types[name] = _span(statement, rest[:type_end])
        if families := _codecs(rest):
            codecs[name] = families
    clauses = _clauses(tokens[end + 1 :])
    return TableDefinition(
        columns=columns,
        types=types,
        codecs=codecs,
        indexes=tuple(indexes),
        clauses={key: _span(statement, value) for key, value in clauses.items()},
        settings=_settings(statement, clauses.get("SETTINGS", [])),
        ttl=_interval(clauses["TTL"]) if "TTL" in clauses else None,
    )


def ttl_from_engine_full(engine_full: str) -> LiveTtl | None:
    """The TTL interval in a ``system.tables.engine_full`` value, or None when it has no TTL."""
    clauses = _clauses(_tokens(engine_full))
    return _interval(clauses["TTL"]) if "TTL" in clauses else None


def settings_from_engine_full(engine_full: str) -> dict[str, str]:
    """The table SETTINGS in a ``system.tables.engine_full`` value, by name, as printed."""
    return _settings(engine_full, _clauses(_tokens(engine_full)).get("SETTINGS", []))


def _normalise_key(expression: str) -> str:
    """A sorting or partition key with backticks, spaces and outer parens out.

    ClickHouse reports ``sorting_key`` without backticks and with its own spacing.
    """
    text = expression.replace("`", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return ", ".join(part.strip() for part in text.split(",") if part.strip())


# -- moves ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TtlMove:
    """What bringing a table's TTL to the declared one takes.

    Attributes:
        statement: The ALTER TABLE that makes the move, empty when nothing moves.
        move: ``<live> -> <declared>``, whole days as a number and ``none`` for no
            TTL, empty when nothing moves.
        expires_rows: Whether the move deletes rows the table keeps today: the new
            TTL is shorter, the table had none, or the live one is not whole days.
    """

    statement: str = ""
    move: str = ""
    expires_rows: bool = False


def ttl_move(
    *, target: str, wanted: int | None, definition: TableDefinition, live: LiveTtl | None
) -> TtlMove:
    """The statement that brings a table whose TTL is *live* to *wanted* days, not run.

    None leaves the table alone. 0, or a *definition* with no TTL clause, removes the
    TTL. Any other value places the declared clause unless *live* already keeps it.

    Args:
        target: The quoted ``database.table``.
        wanted: The days the table should keep.
        definition: The table's CREATE, read.
        live: The table's TTL as read, None when it has none.
    """
    if wanted is None:
        return TtlMove()
    clause = definition.clauses.get("TTL")
    if not wanted or clause is None:
        if live is None:
            return TtlMove()
        return TtlMove(
            statement=f"ALTER TABLE {target} REMOVE TTL", move=f"{live.describe()} -> none"
        )
    if live is not None and live.days == wanted:
        return TtlMove()
    return TtlMove(
        statement=f"ALTER TABLE {target} MODIFY TTL {clause}",
        move=f"{'none' if live is None else live.describe()} -> {wanted}",
        expires_rows=live is None or live.days is None or wanted < live.days,
    )


@dataclass(frozen=True, slots=True)
class SettingsMove:
    """What bringing a table's settings to the declared ones takes.

    Attributes:
        statements: The ALTER TABLE statements that make the move, a MODIFY SETTING
            and then a RESET SETTING, each only when it has work. ClickHouse refuses
            the two in one ALTER (code 62). Empty when nothing moves.
        assignments: ``name = value`` for each setting set, then ``reset name`` for
            each one reset.
    """

    statements: tuple[str, ...] = ()
    assignments: tuple[str, ...] = ()


def settings_move(*, target: str, wanted: Mapping[str, int], live: dict[str, str]) -> SettingsMove:
    """The statements that bring *live* to *wanted*, not run.

    dfe-schemas decides the move: a setting in *wanted* that differs is set, a
    platform-only setting *wanted* omits is reset, and anything else the live table
    carries is left alone. An empty *wanted* moves nothing.

    Args:
        target: The quoted ``database.table``.
        wanted: Setting name to the value the table should carry on this platform.
        live: The table's settings now, as :func:`settings_from_engine_full` reads them.
    """
    if not wanted:
        return SettingsMove()
    realign = realign_settings(wanted, live)
    return SettingsMove(statements=realign.statements(target), assignments=realign.describe())


def describe_settings(moves: tuple[str, ...]) -> list[str]:
    """``set a = 1, b = 2`` and ``reset c`` for a settings move, each only when it has work."""
    resets = [move.removeprefix("reset ") for move in moves if move.startswith("reset ")]
    assignments = [move for move in moves if not move.startswith("reset ")]
    phrases = []
    if assignments:
        phrases.append(f"set {', '.join(assignments)}")
    if resets:
        phrases.append(f"reset {', '.join(resets)}")
    return phrases


def table_target(database: str, table: str) -> str:
    """The quoted ``database.table`` a statement names."""
    return f"{quote_ident(database, what='database')}.{quote_ident(table, what='table name')}"


def _ident(name: str) -> str:
    """An index name bare when ClickHouse reads it bare, else quoted."""
    if name.isascii() and name.isidentifier():
        return name
    return quote_ident(name, what="index name")


def _add_columns(definition: TableDefinition, *, target: str, missing: Sequence[str]) -> list[str]:
    """ADD COLUMN for each of *missing*, each followed by the indexes that read it."""
    statements: list[str] = []
    for name in missing:
        statements.append(
            f"ALTER TABLE {target} ADD COLUMN IF NOT EXISTS `{name}` {definition.columns[name]}"
        )
        statements.extend(
            f"ALTER TABLE {target} ADD {index.sql}"
            for index in definition.indexes
            if name in index.columns
        )
    return statements


# -- server reads ---------------------------------------------------------


def _select(client: Any, sql: str, parameters: dict[str, Any]) -> list:
    """Read server state. A read failure RAISES rather than reporting absence.

    Swallowing it is what turns "ClickHouse is unreachable" into "the table does
    not exist", and then into a CREATE that also fails with the cause discarded.
    ``query`` rather than the wrapper's ``execute``, so a raw clickhouse-connect
    client works too.
    """
    try:
        return list(client.query(sql, parameters=parameters).result_rows)
    except Exception as exc:
        raise SchemaApplyError(f"could not read ClickHouse state: {exc}") from exc


@dataclass(frozen=True, slots=True)
class LiveTable:
    """A table as ClickHouse reports it in ``system.tables``.

    Attributes:
        engine: The engine name, Replicated or Shared prefix included (``ReplicatedMergeTree``).
        ttl: The TTL interval, or None when the table has none.
    """

    engine: str
    ttl: LiveTtl | None

    @property
    def variant(self) -> str:
        """The MergeTree-family variant, without the Replicated or Shared prefix."""
        for prefix in _ENGINE_PREFIXES:
            if self.engine.startswith(prefix) and len(self.engine) > len(prefix):
                return self.engine[len(prefix) :]
        return self.engine


def live_columns(*, client: Any, database: str, table: str) -> dict[str, str]:
    """Every column type of ``database.table`` by column name, empty when the table is absent. Raises SchemaApplyError when ClickHouse cannot be read."""
    rows = _select(
        client,
        "SELECT name, type FROM system.columns WHERE database = {db:String} "
        "AND table = {tbl:String}",
        {"db": database, "tbl": table},
    )
    return {str(name): str(ch_type) for name, ch_type in rows}


def live_tables(client: Any, database: str) -> dict[str, LiveTable]:
    """Every table in *database* by name, in one ``system.tables`` read.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    rows = _select(
        client,
        "SELECT name, engine, engine_full FROM system.tables WHERE database = {db:String}",
        {"db": database},
    )
    return {
        str(name): LiveTable(engine=str(engine), ttl=ttl_from_engine_full(str(full)))
        for name, engine, full in rows
    }


def tables_with_column(*, client: Any, column: str, database: str) -> frozenset[str]:
    """The tables in *database* that carry a column named *column*, in one ``system.columns`` read. Raises SchemaApplyError when ClickHouse cannot be read."""
    rows = _select(
        client,
        "SELECT DISTINCT table FROM system.columns WHERE database = {db:String} "
        "AND name = {col:String}",
        {"col": column, "db": database},
    )
    return frozenset(str(table) for (table,) in rows)


def table_exists(client: Any, database: str, table: str) -> bool:
    """Whether ``database.table`` is in ``system.tables``. Raises SchemaApplyError when ClickHouse cannot be read."""
    return bool(
        _select(
            client,
            "SELECT 1 FROM system.tables WHERE database = {db:String} "
            "AND name = {tbl:String} LIMIT 1",
            {"db": database, "tbl": table},
        )
    )


def _shape(client: Any, database: str, table: str) -> dict[str, str]:
    rows = _select(
        client,
        "SELECT sorting_key, partition_key, engine_full FROM system.tables "
        "WHERE database = {db:String} AND name = {tbl:String}",
        {"db": database, "tbl": table},
    )
    keys = ("sorting_key", "partition_key", "engine_full")
    return {key: str(value or "") for key, value in zip(keys, rows[0], strict=True)} if rows else {}


def settings_move_for(
    client: Any, definition: TableDefinition, *, database: str, table: str
) -> SettingsMove:
    """The statements that give a table the JSON settings *definition* declares.

    A table that declares none is not read at all.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    if not definition.json_settings:
        return SettingsMove()
    live = settings_from_engine_full(_shape(client, database, table).get("engine_full", ""))
    return settings_move(
        target=table_target(database, table), wanted=definition.json_settings, live=live
    )


def _index_moves(
    client: Any, definition: TableDefinition, *, database: str, table: str, kept: Sequence[str]
) -> list[str]:
    """The index DDL that brings the *kept* columns to the indexes *definition* declares.

    A declared index the table lacks is added, and one whose type or granularity
    differs is dropped and added again. An ``idx_<column>`` index, or its ``_key``
    and ``_value`` pair, that the definition does not declare is dropped.
    """
    rows = _select(
        client,
        "SELECT name, type_full, granularity FROM system.data_skipping_indices "
        "WHERE database = {db:String} AND table = {tbl:String}",
        {"db": database, "tbl": table},
    )
    live = {str(name): (str(kind), int(granularity)) for name, kind, granularity in rows}
    target = table_target(database, table)
    statements: list[str] = []
    for column in kept:
        wanted = {i.name: i for i in definition.indexes if column in i.columns}
        managed = {f"idx_{column}", f"idx_{column}_key", f"idx_{column}_value"}
        for name in sorted(managed & live.keys()):
            index = wanted.get(name)
            if index is None or not index.matches(*live[name]):
                statements.append(f"ALTER TABLE {target} DROP INDEX IF EXISTS {_ident(name)}")
        for name, index in wanted.items():
            if name not in live or not index.matches(*live[name]):
                statements.append(f"ALTER TABLE {target} ADD {index.sql}")
    return statements


@dataclass(frozen=True, slots=True)
class TablePlan:
    """What bringing one source table to its CREATE takes, not run.

    Attributes:
        statements: Every statement, in the order to run them.
        exists: Whether the table was present.
        columns_added: The columns the plan adds.
        ttl: The TTL move among the statements.
        settings: The settings move among the statements.
    """

    statements: tuple[str, ...]
    exists: bool
    columns_added: tuple[str, ...] = ()
    ttl: TtlMove = TtlMove()
    settings: SettingsMove = SettingsMove()


def plan_source_table(
    client: Any, create: str, *, database: str, table: str, ttl_days: int | None
) -> TablePlan:
    """The statements that bring a source's table to *create*, its CREATE TABLE.

    An absent table is created. A present one gains the columns it lacks with their
    indexes, has its kept columns' indexes and its TTL moved to the declared ones,
    and then its JSON settings. No column is dropped or retyped.

    Args:
        client: A live ClickHouse client.
        create: The table's CREATE TABLE, which may name the ``{db}`` placeholder.
        database: The database ClickHouse holds the table in.
        table: The table name.
        ttl_days: The days the version keeps. None leaves the TTL alone, and 0 removes it.

    Raises:
        SchemaApplyError: ClickHouse could not be read, or *create* could not be.
    """
    if not table_exists(client, database, table):
        return TablePlan(statements=(create.strip(),), exists=False)
    definition = read_table(create)
    target = table_target(database, table)
    live = live_columns(client=client, database=database, table=table)
    missing = [name for name in definition.columns if name not in live]
    kept = [name for name in definition.columns if name in live]
    statements = _add_columns(definition, target=target, missing=missing)
    statements += _index_moves(client, definition, database=database, table=table, kept=kept)
    # After the column adds: the TTL column may be one of them.
    live_ttl = ttl_from_engine_full(_shape(client, database, table).get("engine_full", ""))
    ttl = ttl_move(target=target, wanted=ttl_days, definition=definition, live=live_ttl)
    settings = settings_move_for(client, definition, database=database, table=table)
    return TablePlan(
        statements=(*statements, *filter(None, [ttl.statement]), *settings.statements),
        exists=True,
        columns_added=tuple(missing),
        ttl=ttl,
        settings=settings,
    )


# -- the applier ----------------------------------------------------------


@dataclass(frozen=True)
class TableChange:
    """What happened to one source table."""

    database: str
    table: str
    action: Action
    columns_added: tuple[str, ...] = ()
    engine: str = ""
    # The TTL move in days, "none -> 90" or "30 -> 90", empty when it did not change.
    ttl: str = ""
    # "name = value" set and "reset name" reset by this pass, empty when none moved.
    settings: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line, in the past tense, for the apply log."""
        target = f"{self.database}.{self.table}"
        if self.action == "created":
            return f"created {target} ENGINE = {self.engine}"
        if self.action == "altered":
            parts = []
            if self.columns_added:
                cols = ", ".join(self.columns_added)
                parts.append(f"added {len(self.columns_added)} column(s) [{cols}]")
            if self.ttl:
                parts.append(f"TTL {self.ttl} days")
            parts.extend(describe_settings(self.settings))
            return f"altered {target}: {', '.join(parts)}"
        return f"unchanged {target}"


@dataclass
class ApplyReport:
    """Everything one source-table pass did."""

    databases_created: list[str] = field(default_factory=list)
    tables: list[TableChange] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """A single line naming the counts -- what the callers log on success."""
        created = sum(1 for t in self.tables if t.action == "created")
        altered = sum(1 for t in self.tables if t.action == "altered")
        unchanged = sum(1 for t in self.tables if t.action == "unchanged")
        return (
            f"{len(self.databases_created)} database(s) created, "
            f"{created} table(s) created, {altered} altered, {unchanged} already current"
        )


def _nullable(ch_type: str) -> bool:
    return "Nullable(" in ch_type


class SchemaApplier:
    """Runs the schema's DDL against one live ClickHouse and records what it ran.

    Construct one per apply pass with a live client and the platform that
    client's probe names, which picks the CREATE DATABASE and a JSON table's
    platform-only settings. ``dry_run`` records the statements without running
    them, and the reads still run, so the plan reflects the real server.
    """

    def __init__(
        self,
        client: Any,
        platform: Platform | str,
        *,
        registry: TypeRegistry | None = None,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._platform = Platform(platform)
        self._registry = registry
        self._dry_run = dry_run
        self.report = ApplyReport()

    def ensure_database(self, database: str) -> bool:
        """Create *database* if absent. Returns whether this pass created it.

        On self-hosted ClickHouse it is a Replicated database created on every
        replica, which replicates each table DDL issued inside it from then on.
        """
        if self._database_exists(database):
            return False
        self._run(create_database(database, self._platform))
        self.report.databases_created.append(database)
        return True

    def table_exists(self, database: str, table: str) -> bool:
        """Whether *table* exists in *database*, read from ``system.tables``."""
        return table_exists(self._client, database, table)

    def ensure_table(
        self,
        database: str,
        table: str,
        columns: list[SchemaColumn],
        config: DDLConfig | None = None,
        *,
        create_ddl: str | None = None,
    ) -> TableChange:
        """Create a source *table*, or bring it to its columns, indexes, TTL and settings.

        Args:
            database: Target database. Must be the REAL name, because a ``{db}``
                placeholder matches no database.
            table: Table name.
            columns: The columns the schema says the table should have.
            config: DDL configuration. Its ``db`` is forced to *database* so the
                generated statements and the reads agree.
            create_ddl: A pre-rendered CREATE TABLE to use instead of generating one.

        Returns:
            The :class:`TableChange` for this table, also appended to the report.
        """
        cfg = replace(config or DDLConfig(), db=database)
        create = create_ddl or self._generator().generate_create_table(
            table_name=table, columns=columns, config=cfg, generated_time=None
        )
        plan = plan_source_table(
            self._client, create, database=database, table=table, ttl_days=cfg.ttl_days
        )
        if plan.ttl.expires_rows:
            logger.warning(f"{database}.{table}: TTL {plan.ttl.move}; rows past the new TTL expire")
        for statement in plan.statements:
            self._run(statement)
        altered = "altered" if plan.statements else "unchanged"
        change = TableChange(
            database=database,
            table=table,
            action=altered if plan.exists else "created",
            columns_added=plan.columns_added,
            engine=engine_clause(parse_engine(cfg.engine)),
            ttl=plan.ttl.move,
            settings=plan.settings.assignments,
        )
        self.report.tables.append(change)
        return change

    def nullability_mismatches(
        self, database: str, table: str, columns: list[SchemaColumn]
    ) -> tuple[str, ...]:
        """Columns of *columns* the table already has with the other nullability.

        :meth:`ensure_table` never retypes an existing column, so these keep the
        nullability they were created with.

        Raises:
            SchemaApplyError: ClickHouse could not be read.
        """
        live = live_columns(client=self._client, database=database, table=table)
        generator = self._generator()
        return tuple(
            col.name
            for col in columns
            if col.name in live
            and _nullable(live[col.name]) != _nullable(generator.resolve_type(col)[0])
        )

    def _generator(self) -> DDLGenerator:
        return DDLGenerator(self._registry or TypeRegistry.default(), platform=self._platform)

    def _database_exists(self, database: str) -> bool:
        return bool(
            _select(
                self._client,
                "SELECT 1 FROM system.databases WHERE name = {db:String} LIMIT 1",
                {"db": database},
            )
        )

    def _run(self, statement: str) -> None:
        """Execute one statement, or record it when planning. Failures RAISE."""
        sql = statement.strip().rstrip(";").strip()
        if not sql:
            return
        self.report.statements.append(sql)
        if self._dry_run:
            return
        try:
            self._client.command(sql, settings=ddl_settings(sql))
        except StatementTooLargeError as exc:
            raise SchemaApplyError(str(exc)) from exc
        except Exception as exc:
            head = sql.splitlines()[0]
            raise SchemaApplyError(f"ClickHouse rejected: {head} -- {exc}") from exc


# -- manifest objects -----------------------------------------------------


@dataclass(frozen=True)
class ObjectOutcome:
    """What happened to one manifest object."""

    id: str
    kind: str
    database: str
    name: str
    action: Action
    checksum: str
    columns_added: tuple[str, ...] = ()
    drift: tuple[str, ...] = ()
    extra_columns: tuple[str, ...] = ()
    reason: str = ""
    # "name = value" for each table setting this pass set, "reset name" for each reset.
    settings: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        """How the object is named in a log line and in the ledger."""
        return f"{self.database}.{self.name}" if self.database else f"topic:{self.name}"

    def _changes(self) -> list[str]:
        changes: list[str] = []
        if self.columns_added:
            added = ", ".join(self.columns_added)
            changes.append(f"added {len(self.columns_added)} column(s) [{added}]")
        changes.extend(describe_settings(self.settings))
        return changes

    def describe(self) -> str:
        """One line, in the past tense, for the apply log."""
        if self.action == "created":
            return f"created {self.qualified}"
        if self.action == "altered":
            changes = self._changes() + [f"applied {message}" for message in self.drift]
            return f"altered {self.qualified}: {'; '.join(changes)}"
        if self.action == "refused":
            refused = f"refused {self.qualified}: {'; '.join(self.drift) or self.reason}"
            return "; ".join([refused, *self._changes()])
        if self.action == "skipped":
            return f"skipped {self.qualified}: {self.reason}"
        return f"unchanged {self.qualified}"


@dataclass
class ManifestReport:
    """Everything one manifest pass did, object by object."""

    outcomes: list[ObjectOutcome] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    @property
    def refused(self) -> list[ObjectOutcome]:
        """Every object whose change was declined, which is the operator's action list."""
        return [outcome for outcome in self.outcomes if outcome.action == "refused"]

    @property
    def changed(self) -> bool:
        """Whether this pass altered the server at all."""
        return any(outcome.action in ("created", "altered") for outcome in self.outcomes)

    def counts(self) -> dict[str, int]:
        """One count per action, every action present so a reader sees the zeroes."""
        counts = dict.fromkeys(("created", "altered", "unchanged", "refused", "skipped"), 0)
        for outcome in self.outcomes:
            counts[outcome.action] += 1
        return counts

    def summary(self) -> str:
        """A single line naming the counts -- what the phase logs on success."""
        counts = self.counts()
        return (
            f"{counts['created']} created, {counts['altered']} altered, "
            f"{counts['unchanged']} already current, {counts['refused']} refused, "
            f"{counts['skipped']} skipped"
        )


class ManifestApplier(SchemaApplier):
    """Applies rendered manifest objects and records each one in the ledger.

    The ledger answers "has this definition changed since it was applied here",
    which a live read cannot, and the catalogue answers "is the object there and the
    shape the definition says", which the ledger cannot. A plain view, a role and
    its grants are re-asserted whenever their checksum moved, and a materialised view
    is created and never replaced, because it holds what it has aggregated.

    ``allow_drift`` turns the refusals ClickHouse can carry out -- a column type
    and a TTL -- into applied ALTERs. An ORDER BY or PARTITION BY change stays
    refused: ClickHouse has no operation for it.
    """

    def __init__(
        self,
        client: Any,
        platform: Platform | str,
        *,
        schemas_version: str,
        dry_run: bool = False,
        allow_drift: bool = False,
    ) -> None:
        super().__init__(client, platform, dry_run=dry_run)
        self._schemas_version = schemas_version
        self._allow_drift = allow_drift
        self._ledger: MigrationLedger | None = None
        self._recorded: dict[tuple[str, str], LedgerRow] = {}
        self._rows: list[list[Any]] = []
        self.manifest = ManifestReport(statements=self.report.statements)

    def attach_ledger(self, ledger: MigrationLedger) -> None:
        """Bind the ledger and read the recorded state.

        Called after the ledger's own table has been applied: it is a manifest
        object like any other, so on a first boot it does not exist until this
        pass has created it.
        """
        self._ledger = ledger
        self._recorded = ledger.checksums()

    def flush(self) -> None:
        """Write every row this pass produced. Nothing applied writes nothing."""
        if self._ledger is None or self._dry_run:
            return
        self._ledger.record(self._rows)
        self._rows = []

    def apply(self, rendered: RenderedObject) -> ObjectOutcome:
        """Converge one object, and record what that took."""
        try:
            return self._apply(rendered)
        except SchemaApplyError:
            if not rendered.optional:
                raise
            return self._record(
                rendered, "skipped", reason="optional object the server would not take"
            )

    def reconcile_ttl(self, rendered: RenderedObject) -> str:
        """Bring one existing table's TTL to the rendered one, and change nothing else.

        The path an admin's change of the deployment default takes for a table that
        follows it, so a render with no TTL removes the live one. Only call it for
        such a table: one that declares its own TTL keeps the drift refusal.

        Returns:
            The move as ``"<live> -> <rendered>"`` in days, or "" when the table is
            absent or already there.

        Raises:
            SchemaApplyError: ClickHouse refused the ALTER or could not be read.
        """
        database = rendered.database or ""
        if rendered.kind != "table" or not self.table_exists(database, rendered.name):
            return ""
        definition = read_table(rendered.statements[0])
        shape = _shape(self._client, database, rendered.name)
        move = ttl_move(
            target=table_target(database, rendered.name),
            wanted=definition.ttl.days if definition.ttl else 0,
            definition=definition,
            live=ttl_from_engine_full(shape.get("engine_full", "")),
        )
        if move.statement:
            self._run(move.statement)
        return move.move

    def _apply(self, rendered: RenderedObject) -> ObjectOutcome:
        database = rendered.database or ""
        recorded = self._recorded.get((database, rendered.name))
        current = recorded is not None and recorded.checksum == rendered.checksum
        kind = rendered.kind
        if kind == "database":
            present = self._database_exists(rendered.name)
        elif kind in ("table", "materialized_view", "view"):
            present = self.table_exists(database, rendered.name)
        elif kind == "role":
            # No cheap live read answers "are these grants the declared set".
            present = current
        else:
            return self._record(rendered, "skipped", reason=f"nothing applies a {kind!r} here")

        if not present:
            for statement in rendered.statements:
                self._run(statement)
            new = kind in ("database", "table", "materialized_view") or recorded is None
            return self._record(rendered, "created" if new else "altered")
        if kind == "table":
            return self._apply_table(rendered)
        if kind == "materialized_view" and recorded is not None and not current:
            return self._record(
                rendered,
                "refused",
                drift=("the SELECT changed, and the view holds what it has aggregated",),
            )
        if kind == "view" and not current:
            for statement in rendered.statements:
                self._run(statement)
            return self._record(rendered, "created" if recorded is None else "altered")
        return self._record(rendered, "unchanged")

    def _apply_table(self, rendered: RenderedObject) -> ObjectOutcome:
        database = rendered.database or ""
        definition = read_table(rendered.statements[0])
        target = table_target(database, rendered.name)
        live = live_columns(client=self._client, database=database, table=rendered.name)
        missing = [name for name in definition.columns if name not in live]
        extra = tuple(name for name in live if name not in definition.columns)
        shape = _shape(self._client, database, rendered.name)
        drift = self._drift(definition, live, shape, database, rendered.name)

        if missing and not rendered.additive:
            refusal = f"{missing[0]} is a new column on an object declared non-additive"
            return self._record(rendered, "refused", drift=(refusal, *(m for _, m in drift)))
        for statement in _add_columns(definition, target=target, missing=missing):
            self._run(statement)
        # Additive like a new column, so a drift refusal never holds it back either.
        live_settings = settings_from_engine_full(shape.get("engine_full", ""))
        settings = settings_move(target=target, wanted=definition.json_settings, live=live_settings)
        for statement in settings.statements:
            self._run(statement)

        if drift and not self._allow_drift:
            action: Action = "refused"
        else:
            for column, message in drift:
                self._apply_drift(definition, target, column, message)
            action = "altered" if missing or settings.statements or drift else "unchanged"
        return self._record(
            rendered,
            action,
            columns_added=tuple(missing),
            drift=tuple(message for _, message in drift),
            extra_columns=extra,
            settings=settings.assignments,
        )

    def _drift(
        self,
        definition: TableDefinition,
        live: dict[str, str],
        shape: dict[str, str],
        database: str,
        table: str,
    ) -> list[tuple[str, str]]:
        """Every non-additive difference, as (what ``--allow-drift`` alters, message).

        The first half is a column whose type moved, ``TTL`` for a TTL, and empty
        for a change ClickHouse cannot make in place.
        """
        drift: list[tuple[str, str]] = []
        for name, declared in definition.types.items():
            if name in live and _squash(live[name]) != _squash(declared):
                drift.append(
                    (name, f"column {name} is {live[name]}, the schema declares {declared}")
                )
        sorting_key = shape.get("sorting_key", "")
        order = definition.clauses.get("ORDER BY", "")
        if order.startswith("(") and _normalise_key(sorting_key) != _normalise_key(order):
            message = f"ORDER BY is ({sorting_key}), the schema declares ({_normalise_key(order)})"
            drift.append(("", message))
        partition_key = shape.get("partition_key", "")
        partition = definition.clauses.get("PARTITION BY")
        if partition and _normalise_key(partition_key) != _normalise_key(partition):
            message = (
                f"PARTITION BY is {partition_key!r}, "
                f"the schema declares {_normalise_key(partition)!r}"
            )
            drift.append(("", message))
        declared_ttl = definition.ttl.days if definition.ttl else None
        live_ttl = ttl_from_engine_full(shape.get("engine_full", ""))
        if declared_ttl is not None and (live_ttl is None or live_ttl.days != declared_ttl):
            if live_ttl is None or live_ttl.days is not None:
                live_days = "none" if live_ttl is None else live_ttl.days
                message = f"TTL is {live_days} days, the schema declares {declared_ttl}"
            else:
                message = f"TTL is {live_ttl.describe()}, the schema declares {declared_ttl} days"
            drift.append(("TTL", message))
        for name, families in definition.codecs.items():
            rows = _select(
                self._client,
                "SELECT compression_codec FROM system.columns WHERE database = {db:String} "
                "AND table = {tbl:String} AND name = {col:String}",
                {"db": database, "tbl": table, "col": name},
            )
            live_families = _codecs(_tokens(str(rows[0][0]))) if rows and rows[0][0] else ()
            if live_families and live_families != families:
                message = (
                    f"column {name} codec is {', '.join(live_families)}, "
                    f"the schema declares {', '.join(families)}"
                )
                drift.append(("", message))
        return drift

    def _apply_drift(
        self, definition: TableDefinition, target: str, column: str, message: str
    ) -> None:
        """Carry out one refused change, under ``--allow-drift``, where ClickHouse can."""
        if column == "TTL":
            self._run(f"ALTER TABLE {target} MODIFY TTL {definition.clauses['TTL']}")
        elif column:
            self._run(f"ALTER TABLE {target} MODIFY COLUMN `{column}` {definition.columns[column]}")
        else:
            logger.warning(
                "schema drift cannot be altered in place; the table has to be rebuilt",
                object=target,
                drift=message,
            )

    def _record(
        self,
        rendered: RenderedObject,
        action: Action,
        *,
        columns_added: tuple[str, ...] = (),
        drift: tuple[str, ...] = (),
        extra_columns: tuple[str, ...] = (),
        reason: str = "",
        settings: tuple[str, ...] = (),
    ) -> ObjectOutcome:
        outcome = ObjectOutcome(
            id=rendered.id,
            kind=rendered.kind,
            database=rendered.database or "",
            name=rendered.name,
            action=action,
            checksum=rendered.checksum,
            columns_added=columns_added,
            drift=drift,
            extra_columns=extra_columns,
            reason=reason,
            settings=settings,
        )
        self.manifest.outcomes.append(outcome)
        if action in ("created", "altered", "unchanged"):
            self._rows.append(
                MigrationLedger.row(
                    database=outcome.database,
                    name=outcome.name,
                    kind=outcome.kind,
                    schemas_version=self._schemas_version,
                    engine_version=engine_version,
                    checksum=rendered.checksum,
                    statement="\n".join(rendered.statements),
                    action=action,
                    platform=str(self._platform),
                )
            )
        return outcome


def log_report(report: ManifestReport, *, prefix: str = "schema") -> None:
    """Log the summary at INFO, each change at INFO, a refusal at ERROR, no-ops at DEBUG."""
    logger.info(f"{prefix}: {report.summary()}")
    for outcome in report.outcomes:
        if outcome.action == "unchanged":
            logger.debug(f"{prefix}: {outcome.describe()}")
        elif outcome.action == "refused":
            logger.error(f"{prefix}: {outcome.describe()}")
        else:
            logger.info(f"{prefix}: {outcome.describe()}")
        if outcome.extra_columns:
            logger.warning(
                "the live table carries columns the schema no longer declares",
                object=outcome.qualified,
                columns=list(outcome.extra_columns),
            )
