#  Project:      dfe-engine
#  File:         schema/applier.py
#  Purpose:      Apply schema DDL to a live ClickHouse, idempotently, and report
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Reconcile a live ClickHouse against the schema, and say what it changed.

``CREATE TABLE IF NOT EXISTS`` is safe to re-run but blind: once the table
exists it is a no-op, so a table missing the columns a newer schema version
added reports success and stays wrong. This module makes re-running a
RECONCILE instead -- create when absent, diff and ``ALTER TABLE ADD COLUMN``
when present -- and returns an :class:`ApplyReport` naming every change.

That report is the point. A schema step that silently no-ops looks exactly like
one that worked, and the three callers (the dfe-infra ArgoCD job, the dfe-docker
one-shot service, the dfe-engine daemon) all run this on every deploy.

Every statement renders through dfe-schemas: the database is the one that
replicates its own DDL, every table takes the argumentless ``Replicated`` engine,
and nothing carries ``ON CLUSTER``, so a multi-node cluster gets the table on
every replica rather than on whichever one the connection happened to land on.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from dfe_schemas.clickhouse import (
    Platform,
    create_database,
    engine_clause,
    parse_engine,
    realign_settings,
)
from scalo.logger import logger

from dfe_engine.clickhouse.statements import StatementTooLargeError, ddl_settings
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerationError, DDLGenerator, quote_ident
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

Action = Literal["created", "altered", "unchanged"]

# engine_full prints an interval as toInterval<Unit>(N); DDL text carries INTERVAL N <UNIT>.
_TTL_INTERVAL_RE = re.compile(
    r"toInterval(?P<fn_unit>[A-Za-z]+)\((?P<fn_amount>\d+)\)"
    r"|INTERVAL\s+(?P<amount>\d+)\s+(?P<unit>[A-Za-z]+)\b"
)

# Seconds in each unit of fixed length; a month, quarter or year depends on the calendar.
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3_600, "day": 86_400, "week": 604_800}
_DAY_SECONDS = 86_400

# What the one DDL shape and Cloud's rewrite put before a variant. Config never declares them.
_ENGINE_PREFIXES = ("Replicated", "Shared")

# One `name = value` in engine_full's SETTINGS clause; a quoted value may hold a comma.
_SETTING_RE = re.compile(r"(?P<name>\w+)\s*=\s*(?P<value>'(?:[^'\\]|\\.)*'|[^,\s]+)")


class SchemaApplyError(Exception):
    """A DDL statement failed, or the server could not be read.

    Raised rather than logged: the standalone entry point is a GATE, and a gate
    that reports success on a failed apply lets the data plane start against a
    database that has no tables.
    """


@dataclass(frozen=True, slots=True)
class LiveTtl:
    """The interval of a table's TTL, as ClickHouse prints it.

    Attributes:
        amount: The interval's count; 0 when no interval could be read from the clause.
        unit: The interval's unit in lower case, such as ``day`` or ``month``; empty
            when no interval could be read from the clause.
    """

    amount: int
    unit: str

    @property
    def days(self) -> int | None:
        """The interval in whole days; None when it is not a whole number of days."""
        seconds = _UNIT_SECONDS.get(self.unit)
        if seconds is None:
            return None
        total = self.amount * seconds
        if total % _DAY_SECONDS:
            return None
        return total // _DAY_SECONDS

    def describe(self) -> str:
        """Whole days as a number (``90``), else the interval as printed (``6 hour``)."""
        days = self.days
        if days is not None:
            return str(days)
        if not self.unit:
            return "unknown"
        return f"{self.amount} {self.unit}"


def ttl_matches(live: LiveTtl | None, days: int | None) -> bool:
    """Whether a table whose TTL is *live* already runs *days*; None is no TTL."""
    if days is None:
        return live is None
    return live is not None and live.days == days


def ttl_from_engine_full(engine_full: str) -> LiveTtl | None:
    """The TTL interval in a ``system.tables.engine_full`` value, or None when it has no TTL."""
    _, sep, ttl_clause = engine_full.partition(" TTL ")
    if not sep:
        return None
    match = _TTL_INTERVAL_RE.search(ttl_clause)
    if match is None:
        return LiveTtl(amount=0, unit="")
    amount = match.group("fn_amount") or match.group("amount")
    unit = match.group("fn_unit") or match.group("unit")
    return LiveTtl(amount=int(amount), unit=unit.lower())


@dataclass(frozen=True, slots=True)
class TtlMove:
    """What bringing a table's TTL to the declared one takes.

    Attributes:
        statement: The ALTER TABLE that makes the move; empty when nothing moves.
        move: ``<live> -> <declared>``, whole days as a number and ``none`` for no
            TTL; empty when nothing moves.
        expires_rows: Whether the move deletes rows the table keeps today: the new
            TTL is shorter, the table had none, or the live one is not whole days.
        skipped: Why a declared TTL that differs from the live one cannot be placed.
    """

    statement: str = ""
    move: str = ""
    expires_rows: bool = False
    skipped: str = ""


def ttl_move(
    *,
    target: str,
    cfg: DDLConfig,
    columns: list[SchemaColumn],
    live: LiveTtl | None,
) -> TtlMove:
    """The statement that brings a table whose TTL is *live* to ``cfg.ttl_days``, not run.

    An undeclared ``ttl_days`` leaves the table alone, and only a declared 0 removes
    the TTL. A declared TTL over a column the table lacks is reported in ``skipped``
    rather than raised, since the columns are what an apply is for.

    Args:
        target: The quoted ``database.table``.
        cfg: The table's DDL configuration.
        columns: The columns the table has once the apply adds what it lacks.
        live: The table's TTL now; None when it has none.
    """
    wanted = cfg.ttl_days
    if wanted is None:
        return TtlMove()
    if wanted == 0:
        if live is None:
            return TtlMove()
        return TtlMove(
            statement=f"ALTER TABLE {target} REMOVE TTL",
            move=f"{live.describe()} -> none",
        )
    if live is not None and live.days == wanted:
        return TtlMove()
    try:
        clause = DDLGenerator.ttl_clause(cfg, columns)
    except (DDLGenerationError, ValueError) as exc:
        return TtlMove(skipped=str(exc))
    if clause is None:
        return TtlMove()
    return TtlMove(
        statement=f"ALTER TABLE {target} MODIFY {clause}",
        move=f"{'none' if live is None else live.describe()} -> {wanted}",
        expires_rows=live is None or live.days is None or wanted < live.days,
    )


def settings_from_engine_full(engine_full: str) -> dict[str, str]:
    """The table SETTINGS in a ``system.tables.engine_full`` value, by name, as printed."""
    _, sep, clause = engine_full.rpartition(" SETTINGS ")
    if not sep:
        return {}
    return {match.group("name"): match.group("value") for match in _SETTING_RE.finditer(clause)}


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
    carries is left alone. A Replicated database replicates either ALTER.

    Args:
        target: The quoted ``database.table``.
        wanted: Setting name to the value the table should carry on this platform.
        live: The table's settings now, as :func:`settings_from_engine_full` reads them.
    """
    realign = realign_settings(wanted, live)
    return SettingsMove(statements=realign.statements(target), assignments=realign.describe())


def table_target(database: str, table: str) -> str:
    """The quoted ``database.table`` a statement names."""
    return f"{quote_ident(database, what='database')}.{quote_ident(table, what='table name')}"


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
    """Every column type of ``database.table`` by column name, empty when the table is absent; raises SchemaApplyError when ClickHouse cannot be read."""
    try:
        rows = client.query(
            "SELECT name, type FROM system.columns WHERE database = {db:String} "
            "AND table = {tbl:String}",
            parameters={"db": database, "tbl": table},
        ).result_rows
    except Exception as exc:
        raise SchemaApplyError(f"could not read ClickHouse state: {exc}") from exc
    return {str(name): str(ch_type) for name, ch_type in rows}


def live_tables(client: Any, database: str) -> dict[str, LiveTable]:
    """Every table in *database* by name, in one ``system.tables`` read.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    try:
        rows = client.query(
            "SELECT name, engine, engine_full FROM system.tables WHERE database = {db:String}",
            parameters={"db": database},
        ).result_rows
    except Exception as exc:
        raise SchemaApplyError(f"could not read ClickHouse state: {exc}") from exc
    return {
        str(name): LiveTable(engine=str(engine), ttl=ttl_from_engine_full(str(full)))
        for name, engine, full in rows
    }


def tables_with_column(*, client: Any, column: str, database: str) -> frozenset[str]:
    """The tables in *database* that carry a column named *column*, in one ``system.columns`` read; raises SchemaApplyError when ClickHouse cannot be read."""
    try:
        rows = client.query(
            "SELECT DISTINCT table FROM system.columns WHERE database = {db:String} "
            "AND name = {col:String}",
            parameters={"col": column, "db": database},
        ).result_rows
    except Exception as exc:
        raise SchemaApplyError(f"could not read ClickHouse state: {exc}") from exc
    return frozenset(str(table) for (table,) in rows)


def read_engine_full(client: Any, database: str, table: str) -> str:
    """``system.tables.engine_full`` for ``database.table``; empty when the table is absent.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    try:
        rows = client.query(
            "SELECT engine_full FROM system.tables WHERE database = {db:String} "
            "AND name = {tbl:String}",
            parameters={"db": database, "tbl": table},
        ).result_rows
    except Exception as exc:
        raise SchemaApplyError(f"could not read ClickHouse state: {exc}") from exc
    return str(rows[0][0]) if rows and rows[0][0] is not None else ""


def json_settings_move(
    client: Any,
    ddl_gen: DDLGenerator,
    *,
    database: str,
    table: str,
    columns: list[SchemaColumn],
) -> SettingsMove:
    """The statements that give a table the settings its JSON columns declare on its platform.

    A table with no JSON column is not read at all, and one already at the declared
    values moves nothing.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    wanted = ddl_gen.json_settings(columns)
    if not wanted:
        return SettingsMove()
    return settings_move(
        target=table_target(database, table),
        wanted=wanted,
        live=settings_from_engine_full(read_engine_full(client, database, table)),
    )


def read_table_ttl(client: Any, database: str, table: str) -> LiveTtl | None:
    """The TTL of ``database.table`` as the server reports it; None when it has none.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    return ttl_from_engine_full(read_engine_full(client, database, table))


def _nullable(ch_type: str) -> bool:
    return "Nullable(" in ch_type


@dataclass(frozen=True)
class TableChange:
    """What happened to one table."""

    database: str
    table: str
    action: Action
    columns_added: tuple[str, ...] = ()
    engine: str = ""
    # The TTL move in days, "none -> 90" or "30 -> 90"; empty when it did not change.
    ttl: str = ""
    # Why a declared TTL that differs from the live one could not be placed; empty otherwise.
    ttl_skipped: str = ""
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


@dataclass
class ApplyReport:
    """Everything one apply pass did, across every database and table."""

    databases_created: list[str] = field(default_factory=list)
    tables: list[TableChange] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether this pass altered the server at all."""
        return bool(self.databases_created) or any(t.action != "unchanged" for t in self.tables)

    def summary(self) -> str:
        """A single line naming the counts -- what the callers log on success."""
        created = sum(1 for t in self.tables if t.action == "created")
        altered = sum(1 for t in self.tables if t.action == "altered")
        unchanged = sum(1 for t in self.tables if t.action == "unchanged")
        return (
            f"{len(self.databases_created)} database(s) created, "
            f"{created} table(s) created, {altered} altered, {unchanged} already current"
        )

    def lines(self) -> list[str]:
        """Per-object detail, database creations first."""
        return [f"created database {db}" for db in self.databases_created] + [
            change.describe() for change in self.tables
        ]


class SchemaApplier:
    """Applies schema DDL to a live ClickHouse and records what changed.

    Construct one per apply pass with a live client and the platform that
    client's probe names, which picks the CREATE DATABASE and a JSON table's
    platform-only settings.

    ``dry_run`` collects the statements into the report without executing any of
    them, so a caller can show the plan. The existence and column reads still
    run: the plan is only meaningful against the real server state.
    """

    def __init__(
        self,
        client: Any,
        platform: Platform,
        *,
        registry: TypeRegistry | None = None,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._platform = platform
        self._ddl_gen = DDLGenerator(registry or TypeRegistry.default(), platform=platform)
        self._dry_run = dry_run
        self.report = ApplyReport()

    # -- databases ---------------------------------------------------

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

    # -- tables ------------------------------------------------------

    def table_exists(self, database: str, table: str) -> bool:
        """Whether *table* exists in *database*, read from ``system.tables``."""
        return self._table_exists(database, table)

    def ensure_table(
        self,
        database: str,
        table: str,
        columns: list[SchemaColumn],
        config: DDLConfig | None = None,
        *,
        create_ddl: str | None = None,
    ) -> TableChange:
        """Create *table*, or add the columns it is missing and reconcile its TTL and settings.

        A declared ``config.ttl_days`` that differs from the live TTL is applied
        with ``MODIFY TTL``, a declared 0 with ``REMOVE TTL``, and an undeclared
        one leaves the live TTL alone. A table with a JSON column is brought to
        dfe-schemas' JSON table settings for this platform: ``MODIFY SETTING``
        where it lacks one, ``RESET SETTING`` where it carries one the platform
        does not take.

        Args:
            database: Target database. Must be the REAL name, because a ``{db}``
                placeholder matches no database.
            table: Table name.
            columns: The columns the schema says the table should have.
            config: DDL configuration. Its ``db`` is forced to *database* so the
                generated statements and the reads agree.
            create_ddl: A pre-rendered CREATE TABLE to use instead of generating
                one. Only consulted when the table is absent.

        Returns:
            The :class:`TableChange` for this table, also appended to the report.
        """
        cfg = config or DDLConfig()
        if cfg.db != database:
            cfg = replace_db(cfg, database)
        engine = engine_clause(parse_engine(cfg.engine))

        if not self._table_exists(database, table):
            ddl = create_ddl or self._ddl_gen.generate_create_table(
                table_name=table, columns=columns, config=cfg, generated_time=None
            )
            self._run(ddl)
            return self._record(
                TableChange(database=database, table=table, action="created", engine=engine)
            )

        existing = self._table_columns(database, table)
        missing = [col for col in columns if col.name not in existing]
        for col in missing:
            self._run(self._ddl_gen.generate_alter_add_column(table, col, cfg))
            for index_stmt in self._ddl_gen.generate_alter_add_indexes(table, col, cfg):
                self._run(index_stmt)

        # After the column adds: the TTL column may be one of them.
        ttl = self._reconcile_ttl(database, table, columns, cfg)
        settings = self._reconcile_settings(database, table, columns)

        return self._record(
            TableChange(
                database=database,
                table=table,
                action="altered"
                if missing or ttl.statement or settings.statements
                else "unchanged",
                columns_added=tuple(col.name for col in missing),
                engine=engine,
                ttl=ttl.move,
                ttl_skipped=ttl.skipped,
                settings=settings.assignments,
            )
        )

    def _reconcile_ttl(
        self,
        database: str,
        table: str,
        columns: list[SchemaColumn],
        cfg: DDLConfig,
    ) -> TtlMove:
        """Bring the live TTL to ``cfg.ttl_days``, as :func:`ttl_move` plans it."""
        move = ttl_move(
            target=table_target(database, table),
            cfg=cfg,
            columns=columns,
            live=self._table_ttl(database, table),
        )
        if move.skipped:
            logger.warning(f"{database}.{table}: TTL not reconciled: {move.skipped}")
        if not move.statement:
            return move
        if move.expires_rows:
            logger.warning(f"{database}.{table}: TTL {move.move}; rows past the new TTL expire")
        else:
            logger.info(f"{database}.{table}: TTL {move.move}")
        self._run(move.statement)
        return move

    def _reconcile_settings(
        self,
        database: str,
        table: str,
        columns: list[SchemaColumn],
    ) -> SettingsMove:
        """Bring the live table settings to the ones its JSON columns declare on this platform."""
        move = json_settings_move(
            self._client, self._ddl_gen, database=database, table=table, columns=columns
        )
        if move.statements:
            logger.info(f"{database}.{table}: {'; '.join(describe_settings(move.assignments))}")
        for statement in move.statements:
            self._run(statement)
        return move

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
        return tuple(
            col.name
            for col in columns
            if col.name in live
            and _nullable(live[col.name]) != _nullable(self._ddl_gen.resolve_type(col)[0])
        )

    # -- materialised views ------------------------------------------

    def ensure_view(self, database: str, name: str, ddl: str) -> TableChange:
        """Create a materialised view if absent.

        Create-only, not a reconcile: a view has no columns to diff, and
        replacing one that already exists would drop whatever it has aggregated.
        Changing a view's SELECT is a deliberate migration, not an apply.
        """
        if self._table_exists(database, name):
            return self._record(TableChange(database=database, table=name, action="unchanged"))
        self._run(ddl)
        return self._record(TableChange(database=database, table=name, action="created"))

    # -- internals ---------------------------------------------------

    def _record(self, change: TableChange) -> TableChange:
        self.report.tables.append(change)
        return change

    def _run(self, statement: str) -> None:
        """Execute one statement, or record it when planning.

        Failures RAISE. The daemon caller wraps the whole pass in its own
        best-effort guard; the gate callers want the non-zero exit.
        """
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

    def _database_exists(self, database: str) -> bool:
        rows = self._select(
            "SELECT 1 FROM system.databases WHERE name = {db:String} LIMIT 1",
            {"db": database},
        )
        return bool(rows)

    def _table_exists(self, database: str, table: str) -> bool:
        rows = self._select(
            "SELECT 1 FROM system.tables WHERE database = {db:String} AND name = {tbl:String} "
            "LIMIT 1",
            {"db": database, "tbl": table},
        )
        return bool(rows)

    def _table_columns(self, database: str, table: str) -> set[str]:
        rows = self._select(
            "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
            {"db": database, "tbl": table},
        )
        return {str(row[0]) for row in rows}

    def _table_ttl(self, database: str, table: str) -> LiveTtl | None:
        return read_table_ttl(self._client, database, table)

    def _select(self, sql: str, parameters: dict[str, Any]) -> list:
        """Read server state. A read failure RAISES rather than reporting absence.

        Swallowing it here is what turns "ClickHouse is unreachable" into
        "the table does not exist", and then into a CREATE that also fails --
        with the original cause already discarded.

        ``query``/``command`` rather than the wrapper's ``execute``: the
        hunt-runner holds a RAW clickhouse-connect client, which has no
        ``execute``. Both shapes carry these two.
        """
        try:
            return list(self._client.query(sql, parameters=parameters).result_rows)
        except Exception as exc:
            raise SchemaApplyError(f"could not read ClickHouse state: {exc}") from exc


def replace_db(cfg: DDLConfig, database: str) -> DDLConfig:
    """A copy of *cfg* targeting *database* -- the caller's config is never mutated."""
    return replace(cfg, db=database)


def log_report(report: ApplyReport, *, prefix: str = "schema") -> None:
    """Log the summary at INFO and each change at INFO, the no-ops at DEBUG."""
    logger.info(f"{prefix}: {report.summary()}")
    for change in report.tables:
        if change.action == "unchanged":
            logger.debug(f"{prefix}: {change.describe()}")
        else:
            logger.info(f"{prefix}: {change.describe()}")
