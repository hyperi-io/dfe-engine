#  Project:      dfe-engine
#  File:         hunt_runner/worker.py
#  Purpose:      Hunt worker execution - windowed query + crash-safe checkpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Execute one hunt run against ClickHouse, incrementally + crash-safe.

A hunt's query carries a ``{window}`` placeholder; the worker substitutes the
incremental predicate on `timestamp_load`, runs it (the query INSERTs matched rows
into its target table), then advances the watermark ONLY after the query commits -
so a crashed pod re-runs the same window from the last committed watermark (no gap,
no loss). The watermark lives in ClickHouse (survivable, independent of the engine).
"""

from __future__ import annotations

from typing import Any

from .checkpoint import predicate, window
from .models import HuntSpec

WINDOW_TOKEN = "{window}"


class CheckpointStore:
    """Per-hunt watermark in ClickHouse (ReplacingMergeTree, latest wins)."""

    def __init__(self, ch: Any, database: str = "dfe_audit", table: str = "hunt_watermark") -> None:
        self._ch = ch
        self._db = database
        self._table = table
        self._fqtn = f"{database}.{table}"

    def ensure(self) -> None:
        self._ch.command(f"CREATE DATABASE IF NOT EXISTS {self._db}")
        self._ch.command(
            f"CREATE TABLE IF NOT EXISTS {self._fqtn} ("
            "hunt_id String, watermark Int64, updated DateTime DEFAULT now()) "
            "ENGINE = ReplacingMergeTree(updated) ORDER BY hunt_id"
        )

    def get(self, hunt_id: str) -> int | None:
        rows = self._ch.query(
            f"SELECT watermark FROM {self._fqtn} FINAL WHERE hunt_id = %(h)s LIMIT 1",
            parameters={"h": hunt_id},
        ).result_rows
        return int(rows[0][0]) if rows else None

    def set(self, hunt_id: str, watermark: int) -> None:
        self._ch.command(
            f"INSERT INTO {self._fqtn} (hunt_id, watermark) VALUES (%(h)s, %(w)s)",
            parameters={"h": hunt_id, "w": watermark},
        )


class HuntWorker:
    """Runs a hunt's windowed query and advances its watermark on success."""

    def __init__(self, ch: Any, checkpoints: CheckpointStore) -> None:
        self._ch = ch
        self._checkpoints = checkpoints

    def run(self, spec: HuntSpec, scheduled_start: int) -> int:
        """Execute the incremental window; return the new watermark (= window end)."""
        last = self._checkpoints.get(spec.hunt_id)
        start, end = window(last, scheduled_start, spec.interval_seconds)
        pred = predicate(start, end, spec.timestamp_field)
        sql = spec.query.replace(WINDOW_TOKEN, pred)
        if sql.strip():
            # The query is an INSERT INTO <target> SELECT ... WHERE {window}.
            self._ch.command(sql)
        # Advance ONLY after the query committed (crash-safe resume).
        self._checkpoints.set(spec.hunt_id, end)
        return end
