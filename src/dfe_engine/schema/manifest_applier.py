#  Project:      dfe-engine
#  File:         schema/manifest_applier.py
#  Purpose:      Apply rendered manifest objects, additively, and refuse drift
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bring a live ClickHouse to the rendered manifest, and say exactly what changed.

The compare has two halves and needs both. The LEDGER answers "has this
definition changed since it was applied here", which a live read cannot: a
reworded comment or a new column in a definition nothing has re-applied is
invisible in ``system.columns``. The live CATALOGUE answers "is the object
actually there and the shape the definition says", which the ledger cannot: a
table somebody altered by hand still has its original row recorded.

What is applied automatically is additive only:

* an absent object is created
* a column the definition adds and the table lacks is added
* a plain view whose SELECT changed is replaced, because it holds no state
* a role, settings profile, quota or grant is re-asserted, because every
  statement in that set converges

What is REFUSED and named:

* a changed column type or codec, an ORDER BY, a PARTITION BY or a TTL change --
  each rewrites or expires data rather than adding to it
* a changed materialised view, because it holds what it has already aggregated
* a live column the definition no longer declares, which is reported and never
  actioned

A refusal is not a failure of the pass. The object is named, the rest of the
manifest still converges, a column the same table gains is still added, and an
operator applies the refused change deliberately with
``dfe schema apply --allow-drift``. A table whose TTL is the deployment default
also takes an admin's change of that default through :meth:`ManifestApplier.reconcile_ttl`.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from dfe_schemas.render import RenderedObject
from scalo.logger import logger

from dfe_engine.clickhouse.statements import StatementTooLargeError, ddl_settings
from dfe_engine.schema.ledger import LedgerRow, MigrationLedger

Action = Literal["created", "altered", "unchanged", "refused", "skipped"]

# system.tables.engine_full renders a day TTL as toIntervalDay(N) or INTERVAL N DAY.
_TTL_DAYS_RE = re.compile(r"toIntervalDay\((\d+)\)|INTERVAL\s+(\d+)\s+DAY\b")
_ORDER_BY_RE = re.compile(r"^ORDER BY \((.*)\)$", re.MULTILINE)
_PARTITION_BY_RE = re.compile(r"^PARTITION BY (.+)$", re.MULTILINE)
# A column line in the rendered body: `name` then the type, up to the first
# clause keyword. Everything the renderer emits starts with a backticked name.
_COLUMN_RE = re.compile(
    r"^`(?P<name>[^`]+)`\s+(?P<type>.+?)(?:\s+(?:DEFAULT|MATERIALIZED|ALIAS|COMMENT|CODEC)\b.*)?$"
)
_CODEC_MARKER = "CODEC("


class ManifestApplyError(Exception):
    """A statement was rejected, or the server could not be read."""


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

    @property
    def qualified(self) -> str:
        """How the object is named in a log line and in the ledger."""
        return f"{self.database}.{self.name}" if self.database else f"topic:{self.name}"

    def describe(self) -> str:
        """One line, in the past tense, for the apply log."""
        if self.action == "created":
            return f"created {self.qualified}"
        if self.action == "altered":
            added = ", ".join(self.columns_added)
            return f"altered {self.qualified}: added {len(self.columns_added)} column(s) [{added}]"
        if self.action == "refused":
            refused = f"refused {self.qualified}: {'; '.join(self.drift) or self.reason}"
            if self.columns_added:
                added = ", ".join(self.columns_added)
                refused += f"; added {len(self.columns_added)} column(s) [{added}]"
            return refused
        if self.action == "skipped":
            return f"skipped {self.qualified}: {self.reason}"
        return f"unchanged {self.qualified}"


@dataclass
class ManifestReport:
    """Everything one apply pass did, object by object."""

    outcomes: list[ObjectOutcome] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    @property
    def refused(self) -> list[ObjectOutcome]:
        """Every object whose change was declined; the operator's action list."""
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


def _column_lines(statement: str) -> dict[str, str]:
    """Column name to its whole declaration, read off the rendered CREATE TABLE body.

    Everything after the backticked name: the type and then whatever DEFAULT,
    COMMENT and CODEC the definition declares. An ALTER built from the type alone
    silently drops the rest.

    Index, projection and constraint lines carry no backticked leading name, so
    they fall out on their own rather than needing a keyword list to exclude.
    """
    body = statement.split("\n(\n", 1)
    if len(body) != 2:
        return {}
    inner = body[1].split("\n)\n", 1)[0]
    columns: dict[str, str] = {}
    for raw in inner.split(",\n"):
        line = raw.strip().rstrip(",").strip()
        match = _COLUMN_RE.match(line)
        if match:
            columns[match.group("name")] = line.split("`", 2)[2].strip()
    return columns


def _column_defs(statement: str) -> dict[str, str]:
    """Column name to declared TYPE alone, for the compare against system.columns."""
    columns: dict[str, str] = {}
    body = statement.split("\n(\n", 1)
    if len(body) != 2:
        return {}
    inner = body[1].split("\n)\n", 1)[0]
    for raw in inner.split(",\n"):
        line = raw.strip().rstrip(",").strip()
        match = _COLUMN_RE.match(line)
        if match:
            columns[match.group("name")] = match.group("type").strip()
    return columns


def _codec_families(text: str) -> tuple[str, ...]:
    """The codec FAMILIES in a ``CODEC(...)`` clause, arguments dropped.

    The server reports the codec with its resolved arguments -- a column typed
    DateTime64 gets ``Delta(8)`` from a declared ``Delta`` -- so comparing the
    text would report every table as drifted forever. The families are the part a
    schema actually declares.
    """
    start = text.upper().find(_CODEC_MARKER)
    if start < 0:
        return ()
    depth = 0
    inner: list[str] = []
    for char in text[start + len(_CODEC_MARKER) - 1 :]:
        if char == "(":
            depth += 1
            if depth == 1:
                continue
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
        inner.append(char)
    families: list[str] = []
    depth = 0
    current: list[str] = []
    for char in [*inner, ","]:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            name = "".join(current).split("(", 1)[0].strip().upper()
            if name:
                families.append(name)
            current = []
            continue
        current.append(char)
    return tuple(families)


def _column_codecs(statement: str) -> dict[str, tuple[str, ...]]:
    """Column name to declared codec families, for the columns that pin one."""
    body = statement.split("\n(\n", 1)
    if len(body) != 2:
        return {}
    inner = body[1].split("\n)\n", 1)[0]
    codecs: dict[str, tuple[str, ...]] = {}
    for raw in inner.split(",\n"):
        line = raw.strip().rstrip(",").strip()
        name = _COLUMN_RE.match(line)
        families = _codec_families(line)
        if name and families:
            codecs[name.group("name")] = families
    return codecs


def _normalise_type(ch_type: str) -> str:
    """A ClickHouse type with its whitespace out.

    The server reports ``DateTime64(3, 'UTC')`` for a declared
    ``DateTime64(3,'UTC')``; a type carries no meaningful space.
    """
    return "".join(ch_type.split())


def _declared_ttl_clause(statement: str) -> str | None:
    """The rendered CREATE TABLE's TTL expressions, without the keyword; None for none."""
    _, sep, tail = statement.partition("\nTTL ")
    if not sep:
        return None
    return tail.split("\nSETTINGS", 1)[0].strip()


def declared_ttl_days(statement: str) -> int | None:
    """The day TTL a rendered CREATE TABLE declares, or None when it declares none."""
    clause = _declared_ttl_clause(statement)
    if clause is None:
        return None
    match = _TTL_DAYS_RE.search(clause)
    return int(match.group(1) or match.group(2)) if match else None


def _normalise_key(expression: str) -> str:
    """A sorting or partition key with backticks, spaces and outer parens out.

    ClickHouse reports ``sorting_key`` without backticks and with its own
    spacing, so a textual compare needs both sides in one form or every table
    reads as drifted.
    """
    text = expression.replace("`", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return ", ".join(part.strip() for part in text.split(",") if part.strip())


class ManifestApplier:
    """Applies rendered manifest objects and records each one in the ledger.

    ``allow_drift`` turns the refusals that ClickHouse can actually carry out --
    a column type change and a TTL change -- into applied ALTERs. An ORDER BY or
    PARTITION BY change stays refused either way: ClickHouse has no operation for
    it, and a table has to be rebuilt.
    """

    def __init__(
        self,
        client: Any,
        *,
        ledger: MigrationLedger | None,
        schemas_version: str,
        engine_version: str,
        topology: str,
        dry_run: bool = False,
        allow_drift: bool = False,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._schemas_version = schemas_version
        self._engine_version = engine_version
        self._topology = topology
        self._dry_run = dry_run
        self._allow_drift = allow_drift
        self._recorded: dict[tuple[str, str], LedgerRow] = {}
        self._rows: list[list[Any]] = []
        self.report = ManifestReport()

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

    # -- entry point ---------------------------------------------------------

    def apply(self, rendered: RenderedObject) -> ObjectOutcome:
        """Converge one object, and record what that took."""
        handler = {
            "database": self._apply_database,
            "table": self._apply_table,
            "materialized_view": self._apply_stateful_view,
            "view": self._apply_view,
            "role": self._apply_role,
        }.get(rendered.kind)
        if handler is None:
            return self._record(
                rendered, "skipped", reason=f"nothing applies a {rendered.kind!r} here"
            )
        try:
            return handler(rendered)
        except ManifestApplyError:
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
            absent or already there. An absent table is created by the next apply.

        Raises:
            ManifestApplyError: ClickHouse refused the ALTER or could not be read.
        """
        database = rendered.database or ""
        if rendered.kind != "table" or not self._table_exists(database, rendered.name):
            return ""
        statement = rendered.statements[0]
        wanted = declared_ttl_days(statement)
        live = self._live_ttl_days(self._live_shape(database, rendered.name).get("engine_full", ""))
        if wanted == live:
            return ""
        target = f"{self._target(database, rendered.name)}{self._on_cluster(statement)}"
        clause = _declared_ttl_clause(statement)
        if clause is None:
            self._run(f"ALTER TABLE {target} REMOVE TTL")
        else:
            self._run(f"ALTER TABLE {target} MODIFY TTL {clause}")
        return f"{'none' if live is None else live} -> {'none' if wanted is None else wanted}"

    # -- per kind ------------------------------------------------------------

    def _apply_database(self, rendered: RenderedObject) -> ObjectOutcome:
        if self._database_exists(rendered.name):
            return self._record(rendered, "unchanged")
        self._run_all(rendered)
        return self._record(rendered, "created")

    def _apply_table(self, rendered: RenderedObject) -> ObjectOutcome:
        database = rendered.database or ""
        if not self._table_exists(database, rendered.name):
            self._run_all(rendered)
            return self._record(rendered, "created")

        statement = rendered.statements[0]
        declared = _column_defs(statement)
        clauses = _column_lines(statement)
        live = self._live_columns(database, rendered.name)

        missing = [name for name in declared if name not in live]
        extra = [name for name in live if name not in declared]
        drift = self._table_drift(rendered, declared, live)

        added: list[str] = []
        for name in missing:
            if not rendered.additive:
                return self._record(
                    rendered,
                    "refused",
                    drift=(f"{name} is a new column on an object declared non-additive", *drift),
                )
            self._run(
                f"ALTER TABLE {self._target(database, rendered.name)}{self._on_cluster(statement)} "
                f"ADD COLUMN IF NOT EXISTS `{name}` {clauses[name]}"
            )
            added.append(name)

        # A new column is additive whatever else drifted, so a refusal never holds one back.
        if drift and not self._allow_drift:
            return self._record(
                rendered,
                "refused",
                columns_added=tuple(added),
                drift=tuple(drift),
                extra_columns=tuple(extra),
            )

        for message in drift:
            self._apply_drift(rendered, message, clauses)

        action: Action = "altered" if added or (drift and self._allow_drift) else "unchanged"
        return self._record(
            rendered,
            action,
            columns_added=tuple(added),
            drift=tuple(drift) if self._allow_drift else (),
            extra_columns=tuple(extra),
        )

    def _apply_stateful_view(self, rendered: RenderedObject) -> ObjectOutcome:
        """A materialised view: create when absent, refuse a changed SELECT.

        Replacing one drops what it has already aggregated, so a changed SELECT
        is a deliberate migration rather than an apply.
        """
        database = rendered.database or ""
        if not self._table_exists(database, rendered.name):
            self._run_all(rendered)
            return self._record(rendered, "created")
        recorded = self._recorded.get((database, rendered.name))
        if recorded is not None and recorded.checksum != rendered.checksum:
            return self._record(
                rendered,
                "refused",
                drift=("the SELECT changed, and the view holds what it has aggregated",),
            )
        return self._record(rendered, "unchanged")

    def _apply_view(self, rendered: RenderedObject) -> ObjectOutcome:
        """A plain view: replaced whenever it is absent or its definition moved."""
        database = rendered.database or ""
        recorded = self._recorded.get((database, rendered.name))
        current = recorded is not None and recorded.checksum == rendered.checksum
        if current and self._table_exists(database, rendered.name):
            return self._record(rendered, "unchanged")
        self._run_all(rendered)
        return self._record(rendered, "created" if recorded is None else "altered")

    def _apply_role(self, rendered: RenderedObject) -> ObjectOutcome:
        """A role, its profile, its quota and its grants. Every statement converges.

        There is no cheap live read that answers "are these grants the declared
        set", so the ledger checksum is the whole compare: an unchanged catalogue
        is a no-op, and a changed one re-asserts the lot.
        """
        database = rendered.database or ""
        recorded = self._recorded.get((database, rendered.name))
        if recorded is not None and recorded.checksum == rendered.checksum:
            return self._record(rendered, "unchanged")
        self._run_all(rendered)
        return self._record(rendered, "created" if recorded is None else "altered")

    # -- drift ---------------------------------------------------------------

    def _table_drift(
        self, rendered: RenderedObject, declared: dict[str, str], live: dict[str, str]
    ) -> list[str]:
        """Every non-additive difference between the definition and the live table."""
        statement = rendered.statements[0]
        database = rendered.database or ""
        drift: list[str] = []

        for name, declared_type in declared.items():
            live_type = live.get(name)
            if live_type is None:
                continue
            if _normalise_type(live_type) != _normalise_type(declared_type):
                drift.append(f"column {name} is {live_type}, the schema declares {declared_type}")

        shape = self._live_shape(database, rendered.name)
        wanted_order = _ORDER_BY_RE.search(statement)
        if wanted_order:
            wanted = _normalise_key(wanted_order.group(1))
            if _normalise_key(shape.get("sorting_key", "")) != wanted:
                drift.append(
                    f"ORDER BY is ({shape.get('sorting_key', '')}), the schema declares ({wanted})"
                )
        wanted_partition = _PARTITION_BY_RE.search(statement)
        if wanted_partition:
            wanted = _normalise_key(wanted_partition.group(1))
            if _normalise_key(shape.get("partition_key", "")) != wanted:
                drift.append(
                    f"PARTITION BY is {shape.get('partition_key', '')!r}, "
                    f"the schema declares {wanted!r}"
                )

        declared_ttl = declared_ttl_days(statement)
        live_ttl = self._live_ttl_days(shape.get("engine_full", ""))
        if declared_ttl is not None and declared_ttl != live_ttl:
            drift.append(
                f"TTL is {'none' if live_ttl is None else live_ttl} days, "
                f"the schema declares {declared_ttl}"
            )

        for name, families in _column_codecs(statement).items():
            live_families = self._live_codec(database, rendered.name, name)
            if live_families and live_families != families:
                drift.append(
                    f"column {name} codec is {', '.join(live_families)}, "
                    f"the schema declares {', '.join(families)}"
                )
        return drift

    def _apply_drift(self, rendered: RenderedObject, message: str, clauses: dict[str, str]) -> None:
        """Carry out one refused change, under ``--allow-drift``.

        Only the two ClickHouse has an operation for. An ORDER BY or PARTITION BY
        change needs the table rebuilt, so it is left named rather than half-done.
        """
        database = rendered.database or ""
        target = (
            f"{self._target(database, rendered.name)}{self._on_cluster(rendered.statements[0])}"
        )
        if message.startswith("column ") and " is " in message and "codec" not in message:
            name = message.split(" ", 2)[1]
            self._run(f"ALTER TABLE {target} MODIFY COLUMN `{name}` {clauses[name]}")
            return
        if message.startswith("TTL is"):
            clause = _declared_ttl_clause(rendered.statements[0])
            if clause:
                self._run(f"ALTER TABLE {target} MODIFY TTL {clause}")
            return
        logger.warning(
            "schema drift cannot be altered in place; the table has to be rebuilt",
            object=f"{database}.{rendered.name}",
            drift=message,
        )

    # -- server reads --------------------------------------------------------

    def _live_columns(self, database: str, table: str) -> dict[str, str]:
        rows = self._select(
            "SELECT name, type FROM system.columns "
            "WHERE database = {db:String} AND table = {tbl:String}",
            {"db": database, "tbl": table},
        )
        return {str(row[0]): str(row[1]) for row in rows}

    def _live_codec(self, database: str, table: str, column: str) -> tuple[str, ...]:
        rows = self._select(
            "SELECT compression_codec FROM system.columns WHERE database = {db:String} "
            "AND table = {tbl:String} AND name = {col:String}",
            {"db": database, "tbl": table, "col": column},
        )
        if not rows or rows[0][0] is None:
            return ()
        return _codec_families(str(rows[0][0]))

    def _live_shape(self, database: str, table: str) -> dict[str, str]:
        rows = self._select(
            "SELECT sorting_key, partition_key, engine_full FROM system.tables "
            "WHERE database = {db:String} AND name = {tbl:String}",
            {"db": database, "tbl": table},
        )
        if not rows:
            return {}
        return {
            "sorting_key": str(rows[0][0] or ""),
            "partition_key": str(rows[0][1] or ""),
            "engine_full": str(rows[0][2] or ""),
        }

    @staticmethod
    def _live_ttl_days(engine_full: str) -> int | None:
        _, sep, clause = engine_full.partition(" TTL ")
        if not sep:
            return None
        match = _TTL_DAYS_RE.search(clause)
        return int(match.group(1) or match.group(2)) if match else None

    def _database_exists(self, database: str) -> bool:
        return bool(
            self._select(
                "SELECT 1 FROM system.databases WHERE name = {db:String} LIMIT 1",
                {"db": database},
            )
        )

    def _table_exists(self, database: str, table: str) -> bool:
        return bool(
            self._select(
                "SELECT 1 FROM system.tables WHERE database = {db:String} "
                "AND name = {tbl:String} LIMIT 1",
                {"db": database, "tbl": table},
            )
        )

    def _select(self, sql: str, parameters: dict[str, Any]) -> list:
        """Read server state. A read failure RAISES rather than reporting absence.

        Swallowing it is what turns "ClickHouse is unreachable" into "the table
        does not exist", and then into a CREATE that also fails with the original
        cause already discarded.
        """
        try:
            return list(self._client.query(sql, parameters=parameters).result_rows)
        except Exception as exc:
            raise ManifestApplyError(f"could not read ClickHouse state: {exc}") from exc

    # -- statements ----------------------------------------------------------

    @staticmethod
    def _target(database: str, name: str) -> str:
        return f"`{database}`.`{name}`"

    @staticmethod
    def _on_cluster(statement: str) -> str:
        """The ON CLUSTER suffix the rendered statement carries, for an ALTER."""
        head = statement.splitlines()[0]
        _, sep, tail = head.partition(" ON CLUSTER ")
        return f" ON CLUSTER {tail.strip()}" if sep else ""

    def _run_all(self, rendered: RenderedObject) -> None:
        for statement in rendered.statements:
            self._run(statement)

    def _run(self, statement: str) -> None:
        sql = statement.strip().rstrip(";").strip()
        if not sql:
            return
        self.report.statements.append(sql)
        if self._dry_run:
            return
        try:
            self._client.command(sql, settings=ddl_settings(sql))
        except StatementTooLargeError as exc:
            raise ManifestApplyError(str(exc)) from exc
        except Exception as exc:
            raise ManifestApplyError(
                f"ClickHouse rejected: {sql.splitlines()[0]} -- {exc}"
            ) from exc

    # -- recording -----------------------------------------------------------

    def _record(
        self,
        rendered: RenderedObject,
        action: Action,
        *,
        columns_added: tuple[str, ...] = (),
        drift: tuple[str, ...] = (),
        extra_columns: tuple[str, ...] = (),
        reason: str = "",
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
        )
        self.report.outcomes.append(outcome)
        if action in ("created", "altered", "unchanged"):
            self._rows.append(
                MigrationLedger.row(
                    database=outcome.database,
                    name=outcome.name,
                    kind=outcome.kind,
                    schemas_version=self._schemas_version,
                    engine_version=self._engine_version,
                    checksum=rendered.checksum,
                    statement="\n".join(rendered.statements),
                    action=action,
                    topology=self._topology,
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
