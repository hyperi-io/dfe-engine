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

Every statement takes its engine and its ``ON CLUSTER`` from the injected
:class:`~dfe_engine.schema.engine_resolver.EngineResolver`, so a multi-node
cluster gets the table on every replica rather than on whichever one the
connection happened to land on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from scalo.logger import logger

from dfe_engine.clickhouse.statements import StatementTooLargeError, ddl_settings
from dfe_engine.schema.engine_resolver import EngineResolver, parse_engine
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerationError, DDLGenerator, quote_ident
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

Action = Literal["created", "altered", "unchanged"]

# system.tables.engine_full renders a day TTL as toIntervalDay(N) or INTERVAL N DAY.
_TTL_DAYS_RE = re.compile(r"toIntervalDay\((\d+)\)|INTERVAL\s+(\d+)\s+DAY\b")


class SchemaApplyError(Exception):
    """A DDL statement failed, or the server could not be read.

    Raised rather than logged: the standalone entry point is a GATE, and a gate
    that reports success on a failed apply lets the data plane start against a
    database that has no tables.
    """


@dataclass(frozen=True)
class TableChange:
    """What happened to one table."""

    database: str
    table: str
    action: Action
    columns_added: tuple[str, ...] = ()
    engine: str = ""
    on_cluster: str = ""
    # The TTL move in days, "none -> 90" or "30 -> 90"; empty when it did not change.
    ttl: str = ""

    def describe(self) -> str:
        """One line, in the past tense, for the apply log."""
        target = f"{self.database}.{self.table}"
        cluster = self.on_cluster.strip() or "no cluster"
        if self.action == "created":
            return f"created {target} ENGINE = {self.engine} ({cluster})"
        if self.action == "altered":
            parts = []
            if self.columns_added:
                cols = ", ".join(self.columns_added)
                parts.append(f"added {len(self.columns_added)} column(s) [{cols}]")
            if self.ttl:
                parts.append(f"TTL {self.ttl} days")
            return f"altered {target}: {', '.join(parts)}"
        return f"unchanged {target}"


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

    Construct one per apply pass with a live client and a resolver built from
    that same client -- sensing is what emits ``ON CLUSTER``, and a resolver
    without a client cannot sense.

    ``dry_run`` collects the statements into the report without executing any of
    them, so a caller can show the plan. The existence and column reads still
    run: the plan is only meaningful against the real server state.
    """

    def __init__(
        self,
        client: Any,
        resolver: EngineResolver,
        *,
        registry: TypeRegistry | None = None,
        dry_run: bool = False,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._ddl_gen = DDLGenerator(registry or TypeRegistry.default(), resolver=resolver)
        self._dry_run = dry_run
        self.report = ApplyReport()

    # -- databases ---------------------------------------------------

    def ensure_database(self, database: str) -> bool:
        """Create *database* if absent. Returns whether this pass created it.

        Cluster-wide when the resolver senses a cluster: without that, ON CLUSTER
        table DDL lands on nodes that have no database to put it in.
        """
        if self._database_exists(database):
            return False
        on_cluster = self._resolver.resolve(parse_engine("MergeTree"), database).on_cluster
        self._run(f"CREATE DATABASE IF NOT EXISTS {database}{on_cluster}")
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
        """Create *table*, or add whatever columns it is missing and reconcile its TTL.

        A declared ``config.ttl_days`` that differs from the live TTL is applied
        with ``MODIFY TTL``, a declared 0 with ``REMOVE TTL``; an undeclared one
        leaves the live TTL alone.

        Args:
            database: Target database. Must be the REAL name -- the resolver
                senses on it, and a ``{db}`` placeholder matches no database.
            table: Table name.
            columns: The columns the schema says the table should have.
            config: DDL configuration. Its ``db`` is forced to *database* so the
                generated statements and the sensing agree.
            create_ddl: A pre-rendered CREATE TABLE to use instead of generating
                one. Only consulted when the table is absent.

        Returns:
            The :class:`TableChange` for this table, also appended to the report.
        """
        cfg = config or DDLConfig()
        if cfg.db != database:
            cfg = replace_db(cfg, database)
        resolved = self._resolver.resolve(parse_engine(cfg.engine), database)

        if not self._table_exists(database, table):
            ddl = create_ddl or self._ddl_gen.generate_create_table(
                table_name=table, columns=columns, config=cfg, generated_time=None
            )
            self._run(ddl)
            return self._record(
                TableChange(
                    database=database,
                    table=table,
                    action="created",
                    engine=resolved.clause,
                    on_cluster=resolved.on_cluster,
                )
            )

        existing = self._table_columns(database, table)
        missing = [col for col in columns if col.name not in existing]
        for col in missing:
            self._run(self._ddl_gen.generate_alter_add_column(table, col, cfg))
            for index_stmt in self._ddl_gen.generate_alter_add_indexes(table, col, cfg):
                self._run(index_stmt)

        # After the column adds: the TTL column may be one of them.
        on_cluster = f" ON CLUSTER {cfg.cluster}" if cfg.cluster else resolved.on_cluster
        ttl = self._reconcile_ttl(database, table, columns, cfg, on_cluster)

        return self._record(
            TableChange(
                database=database,
                table=table,
                action="altered" if missing or ttl else "unchanged",
                columns_added=tuple(col.name for col in missing),
                engine=resolved.clause,
                on_cluster=resolved.on_cluster,
                ttl=ttl,
            )
        )

    def _reconcile_ttl(
        self,
        database: str,
        table: str,
        columns: list[SchemaColumn],
        cfg: DDLConfig,
        on_cluster: str,
    ) -> str:
        """Bring the live TTL to ``cfg.ttl_days``. Returns the move, or "" for none.

        An undeclared ``ttl_days`` leaves the table alone; only a declared 0 removes the TTL.
        A declared TTL over a column the table lacks is logged and skipped rather
        than failing the apply, since the columns are the gate's real job.
        """
        wanted = cfg.ttl_days
        if wanted is None:
            return ""
        live = self._table_ttl_days(database, table)
        target = f"{quote_ident(database, what='database')}.{quote_ident(table, what='table name')}"
        # A live 0-day TTL expires every row, so it is removed rather than matched.
        if wanted == 0:
            if live is None:
                return ""
            logger.info(f"{database}.{table}: TTL {live} -> none; rows are kept forever")
            self._run(f"ALTER TABLE {target}{on_cluster} REMOVE TTL")
            return f"{live} -> none"
        if live == wanted:
            return ""
        try:
            clause = DDLGenerator._ttl_clause(cfg, columns)
        except (DDLGenerationError, ValueError) as exc:
            logger.warning(f"{database}.{table}: TTL not reconciled: {exc}")
            return ""
        if clause is None:
            return ""
        move = f"{'none' if live is None else live} -> {wanted}"
        if live is not None and wanted < live:
            logger.warning(
                f"{database}.{table}: TTL shortened {move} days; rows older than "
                f"{wanted} days will expire"
            )
        else:
            logger.info(f"{database}.{table}: TTL {move} days")
        self._run(f"ALTER TABLE {target}{on_cluster} MODIFY {clause}")
        return move

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

    def _table_ttl_days(self, database: str, table: str) -> int | None:
        """The live TTL in days, or None when the table has no day-based TTL."""
        rows = self._select(
            "SELECT engine_full FROM system.tables WHERE database = {db:String} "
            "AND name = {tbl:String}",
            {"db": database, "tbl": table},
        )
        engine_full = str(rows[0][0]) if rows and rows[0][0] is not None else ""
        _, sep, ttl_clause = engine_full.partition(" TTL ")
        if not sep:
            return None
        match = _TTL_DAYS_RE.search(ttl_clause)
        if match is None:
            return None
        return int(match.group(1) or match.group(2))

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
