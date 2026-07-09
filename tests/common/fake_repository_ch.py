#  Project:      dfe-engine
#  File:         fake_repository_ch.py
#  Purpose:      In-memory fake ClickHouse client for RepositoryStore tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""In-memory stand-in for ClickHouseClientWrapper covering RepositoryStore SQL.

Implements exactly what the store issues: the DDL CREATEs, the
INSERT-with-data path, and the two SELECT shapes (get by key vs list a
namespace). SELECT honours ReplacingMergeTree(updated_at, is_deleted)
FINAL semantics: latest updated_at wins per key (last insert wins on
ties, matching ClickHouse merge behaviour), tombstones hide the row.
"""

from __future__ import annotations

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

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.ddl: list[str] = []
        self.select_params: list[dict[str, Any]] = []

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
