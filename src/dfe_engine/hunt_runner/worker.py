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

from dfe_engine.clickhouse.attribution import DfeQueryTags

from .ch_coordinator import ChCoordinator
from .checkpoint import predicate, window
from .models import HuntSpec

WINDOW_TOKEN = "{window}"


def query_settings(hunt_id: str, workload: str = "") -> dict[str, str]:
    """Per-query ClickHouse settings that attribute (and optionally class) a hunt run.

    ``log_comment`` is a JSON :class:`DfeQueryTags` payload (feature=hunts, id=the
    hunt) so ``system.query_log`` -> the ``query_log_archive`` MV KEEPS the row (the
    MV drops non-JSON log_comments) and the cost leaderboard attributes cost back to
    the hunt. ``workload`` puts the query in a CH WORKLOAD for server-side
    fair-share, but is only set when a workload name is CONFIGURED: setting an
    UNDEFINED workload errors on the server, and the "hunts" workload is not
    provisioned until the smoothing backstop lands
    (docs/data-plane/hunt-schedule-smoothing.md), so it defaults OFF. Pure, so it is
    unit-testable; the real attribution is verified in the live-CH Phase A test.
    """
    settings = {"log_comment": DfeQueryTags(feature="hunts", kind="hunt", id=hunt_id).to_json()}
    if workload:
        settings["workload"] = workload
    return settings


class HuntWorker:
    """Runs a hunt's windowed query and advances its watermark on success."""

    def __init__(self, ch: Any, coordinator: ChCoordinator, workload: str = "") -> None:
        self._ch = ch
        self._coord = coordinator
        # Optional CH WORKLOAD name for server-side fair-share; empty = do not set it
        # (an undefined workload errors). Provisioned later (the v2 smoothing backstop).
        self._workload = workload

    def run(self, spec: HuntSpec, scheduled_start: int) -> int:
        """Execute the incremental window; return the new watermark (= window end)."""
        last = self._coord.get_watermark(spec.hunt_id)
        start, end = window(last, scheduled_start, spec.interval_seconds)
        pred = predicate(start, end, spec.timestamp_field)
        sql = spec.query.replace(WINDOW_TOKEN, pred)
        if sql.strip():
            # INSERT INTO <target> SELECT ... WHERE {window}. log_comment attributes
            # the query in system.query_log; workload (if configured) puts it in a CH
            # fair-share class.
            self._ch.command(sql, settings=query_settings(spec.hunt_id, self._workload))
        # Advance ONLY after the query committed (crash-safe resume).
        self._coord.set_watermark(spec.hunt_id, end)
        return end
