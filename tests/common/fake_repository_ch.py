#  Project:      dfe-engine
#  File:         fake_repository_ch.py
#  Purpose:      In-memory fake ClickHouse client for RepositoryStore tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""In-memory stand-in for ClickHouseClientWrapper covering RepositoryStore SQL.

Implements exactly what the store issues: the presence read ``ensure_schema``
makes, the INSERT-with-data path, and the two SELECT shapes (get by key vs list
a namespace). SELECT honours ReplacingMergeTree(updated_at, is_deleted) FINAL
semantics: latest updated_at wins per key (last insert wins on ties, matching
ClickHouse merge behaviour), tombstones hide the row.

The store no longer creates its own table -- the engine's schema phase does --
so the fake is constructed with the tables that phase has already applied.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

_ROW_COLS = (
    "scope",
    "scope_id",
    "namespace",
    "key",
    "content_type",
    "value",
    "size",
    "updated_by",
    "updated_at",
    "is_deleted",
)


class FakeRepositoryCH:
    """Fake ClickHouse client wrapper for the repository table."""

    def __init__(self, *, applied: set[tuple[str, str]] | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self.ddl: list[str] = []
        self.select_params: list[dict[str, Any]] = []
        self.presence_reads: list[dict[str, Any]] = []
        # What the engine's schema phase has already applied on this server.
        self.databases: set[str] = {db for db, _ in (applied or set())}
        self.tables: set[tuple[str, str]] = set(applied or set())

    # ── the applier's surface: command + query ────────────────

    def command(self, statement: str, *args: Any, **kwargs: Any):
        """DDL passthrough, recording what it created."""
        self.ddl.append(statement.strip())
        db = re.search(r"CREATE DATABASE IF NOT EXISTS (\S+)", statement)
        if db:
            self.databases.add(db.group(1))
        tbl = re.search(r"CREATE TABLE IF NOT EXISTS (\S+)\.(\S+)", statement)
        if tbl:
            self.tables.add((tbl.group(1), tbl.group(2)))
        return []

    def query(self, sql: str, *args: Any, parameters: Any = None, **kwargs: Any):
        """The state reads, returning a QueryResult-shaped object."""
        params = parameters or {}
        # The engine resolver's sensing probes: no cloud_mode and no cluster
        # macros, so it classifies this fake as a single node.
        if "system.settings" in sql or "system.macros" in sql:
            return SimpleNamespace(result_rows=[])
        if "system.databases" in sql and "engine" in sql:
            return SimpleNamespace(result_rows=[])
        if "system.databases" in sql:
            rows = [(1,)] if params.get("db") in self.databases else []
        elif "system.tables" in sql and "names" in params:
            # The presence read: which of the named objects this server carries.
            self.presence_reads.append(dict(params))
            wanted = set(params["names"])
            rows = [
                (name,)
                for db, name in sorted(self.tables)
                if db == params.get("db") and name in wanted
            ]
        elif "system.tables" in sql:
            rows = [(1,)] if (params.get("db"), params.get("tbl")) in self.tables else []
        elif "system.columns" in sql:
            rows = [(name,) for name in _ROW_COLS]
        else:
            raise AssertionError(f"unexpected query: {sql}")
        return SimpleNamespace(result_rows=rows)

    def execute(self, query: str, *args: Any, parameters: Any = None, **kwargs: Any):
        q = query.strip()
        q_upper = q.upper()
        if q_upper.startswith("CREATE"):
            self.ddl.append(q)
            return []
        if q_upper.startswith("INSERT"):
            assert args, "store must INSERT with a data list"
            assert isinstance(args[0], list), "store must INSERT with a data list"
            for row in args[0]:
                self.rows.append(dict(zip(_ROW_COLS, row, strict=True)))
            return []
        if q_upper.startswith("SELECT"):
            assert isinstance(parameters, dict), "store must bind SELECT parameters"
            self.select_params.append(parameters)
            return self._select(parameters)
        raise AssertionError(f"unexpected query: {q}")

    # ── FINAL semantics ───────────────────────────────────────

    def _live_rows(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in self.rows:
            if row["scope"] != params["scope"]:
                continue
            if row["scope_id"] != params["scope_id"]:
                continue
            if row["namespace"] != params["namespace"]:
                continue
            if "key" in params and row["key"] != params["key"]:
                continue
            group = (row["scope"], row["scope_id"], row["namespace"], row["key"])
            current = latest.get(group)
            if current is None or row["updated_at"] >= current["updated_at"]:
                latest[group] = row
        return [r for r in latest.values() if r["is_deleted"] == 0]

    def _select(self, params: dict[str, Any]) -> list[tuple]:
        live = self._live_rows(params)
        if "key" in params:
            # RepositoryStore._GET_SQL column order
            return [
                (r["content_type"], r["value"], r["size"], r["updated_by"], r["updated_at"])
                for r in live
            ][:1]
        # RepositoryStore._LIST_SQL column order
        live.sort(key=lambda r: r["key"])
        return [
            (r["key"], r["content_type"], r["size"], r["updated_by"], r["updated_at"]) for r in live
        ]
