#  Project:      dfe-engine
#  File:         schema/ledger.py
#  Purpose:      Read and write schema_migrations, the record of what was applied
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The applied-object ledger, so a cluster can answer which schema it is on.

Before this the apply report was printed and discarded, and the only way to ask
"is this cluster on the pinned dfe-schemas release" was to re-run the apply and
watch what it did. One row per object records its kind, the dfe-schemas release
it was rendered from, the engine release that applied it, the checksum of the
normalised statement and the statement itself.

The checksum is what makes the next pass cheap and truthful: it is taken with the
topology token removed, so one schema reads as one checksum whether it was
applied to a single node or fanned over a cluster.

The table is a manifest object like any other and is created by the same applier
before anything else, so the engine carries no DDL even for its own bookkeeping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dfe_engine.clickhouse.quoting import quote_identifier

_COLUMNS = (
    "database",
    "object",
    "kind",
    "schemas_version",
    "engine_version",
    "checksum",
    "statement",
    "action",
    "topology",
)


class LedgerError(Exception):
    """The ledger could not be read or written."""


@dataclass(frozen=True)
class LedgerRow:
    """The latest recorded state of one object."""

    database: str
    object: str
    kind: str
    schemas_version: str
    engine_version: str
    checksum: str
    action: str
    topology: str


class MigrationLedger:
    """Reads and writes ``schema_migrations``.

    Construct with the table's real name, which the caller reads off the rendered
    manifest object -- this module names no table of its own.
    """

    def __init__(self, client: Any, *, database: str, table: str) -> None:
        self._client = client
        self._database = database
        self._table = table

    def checksums(self) -> dict[tuple[str, str], LedgerRow]:
        """The latest row per (database, object), keyed for the compare step."""
        target = f"{quote_identifier(self._database)}.{quote_identifier(self._table)}"
        sql = (
            "SELECT database, object, argMax(kind, applied_at), "  # noqa: S608 - quoted database and manifest table
            "argMax(schemas_version, applied_at), argMax(engine_version, applied_at), "
            "argMax(checksum, applied_at), argMax(action, applied_at), "
            f"argMax(topology, applied_at) FROM {target} "
            "GROUP BY database, object"
        )
        try:
            rows = list(self._client.query(sql).result_rows)
        except Exception as exc:
            raise LedgerError(f"could not read the migration ledger: {exc}") from exc
        recorded: dict[tuple[str, str], LedgerRow] = {}
        for row in rows:
            entry = LedgerRow(
                database=str(row[0]),
                object=str(row[1]),
                kind=str(row[2]),
                schemas_version=str(row[3]),
                engine_version=str(row[4]),
                checksum=str(row[5]),
                action=str(row[6]),
                topology=str(row[7]),
            )
            recorded[(entry.database, entry.object)] = entry
        return recorded

    def record(self, rows: list[list[Any]]) -> None:
        """Write one row per applied object. Nothing to write is not an error."""
        if not rows:
            return
        try:
            self._client.insert(
                self._table, rows, column_names=list(_COLUMNS), database=self._database
            )
        except Exception as exc:
            raise LedgerError(f"could not write the migration ledger: {exc}") from exc

    @staticmethod
    def row(
        *,
        database: str,
        name: str,
        kind: str,
        schemas_version: str,
        engine_version: str,
        checksum: str,
        statement: str,
        action: str,
        topology: str,
    ) -> list[Any]:
        """One ledger row, in the column order :meth:`record` inserts."""
        return [
            database,
            name,
            kind,
            schemas_version,
            engine_version,
            checksum,
            statement,
            action,
            topology,
        ]
