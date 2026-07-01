#  Project:      dfe-engine
#  File:         hunt_runner/worker.py
#  Purpose:      Hunt worker execution - windowed query + crash-safe checkpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Execute one hunt run against ClickHouse, incrementally + crash-safe.

A hunt's query carries a ``{window}`` placeholder; the worker substitutes the
incremental predicate on ``timestamp_load``, runs it (the query INSERTs matched
rows into its target table), then advances the watermark ONLY after the query
commits - so a crashed pod re-runs the same window from the last committed
watermark (no gap, no loss). The watermark lives in ClickHouse via the
ChCoordinator (survivable, independent of the engine).
"""

from __future__ import annotations

from typing import Any

from .ch_coordinator import ChCoordinator
from .checkpoint import predicate, window
from .models import HuntSpec

WINDOW_TOKEN = "{window}"


class HuntWorker:
    """Runs a hunt's windowed query and advances its watermark on success."""

    def __init__(self, ch: Any, coordinator: ChCoordinator) -> None:
        self._ch = ch
        self._coord = coordinator

    def run(self, spec: HuntSpec, scheduled_start: int) -> int:
        """Execute the incremental window; return the new watermark (= window end)."""
        last = self._coord.get_watermark(spec.hunt_id)
        start, end = window(last, scheduled_start, spec.interval_seconds)
        pred = predicate(start, end, spec.timestamp_field)
        sql = spec.query.replace(WINDOW_TOKEN, pred)
        if sql.strip():
            # The query is an INSERT INTO <target> SELECT ... WHERE {window}.
            self._ch.command(sql)
        # Advance ONLY after the query committed (crash-safe resume).
        self._coord.set_watermark(spec.hunt_id, end)
        return end
