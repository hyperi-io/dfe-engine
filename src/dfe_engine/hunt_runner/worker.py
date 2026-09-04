#  Project:      dfe-engine
#  File:         hunt_runner/worker.py
#  Purpose:      Hunt worker execution - windowed query + crash-safe checkpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Execute one hunt run against ClickHouse, incrementally + crash-safe.

Each of a hunt's statements carries a ``{window}`` placeholder; the worker
substitutes the incremental predicate on the hunt's timestamp field, runs them (each
INSERTs matched rows into its target table), then advances the watermark ONLY after
they commit - so a crashed pod re-runs the same window from the last committed
watermark (no gap, no loss). The watermark lives in ClickHouse via the
ChCoordinator (survivable, independent of the engine).

A hunt with nothing to run RAISES. Advancing the watermark past a window nothing
scanned is a clean-looking success that detects nothing and leaves no evidence.
"""

from __future__ import annotations

from typing import Any

from scalo.logger import logger

from dfe_engine.clickhouse.attribution import DfeQueryTags

from .ch_coordinator import ChCoordinator
from .checkpoint import predicate, window
from .models import HuntSpec

WINDOW_TOKEN = "{window}"


class EmptyHuntQuery(RuntimeError):
    """A hunt that compiled to no statements, so there is nothing to execute."""


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
        """Execute the incremental window; return the new watermark (= window end).

        Raises:
            EmptyHuntQuery: the hunt has no statement to run, so its rules named no
                rule file the compiler could resolve. The watermark does NOT advance.
        """
        statements = [sql for sql in spec.queries if sql.strip()]
        if not statements:
            logger.error(
                f"hunt {spec.hunt_id} has no query to run: its rules compiled to nothing, "
                "so the watermark is held and this fire is a failure, not a clean run"
            )
            raise EmptyHuntQuery(f"hunt {spec.hunt_id} compiled to no statements")

        last = self._coord.get_watermark(spec.hunt_id)
        start, end = window(last, scheduled_start, spec.interval_seconds)
        pred = predicate(start, end, spec.timestamp_field)
        settings = query_settings(spec.hunt_id, self._workload)
        for sql in statements:
            # INSERT INTO <target> SELECT ... WHERE {window}. log_comment attributes
            # the query in system.query_log; workload (if configured) puts it in a CH
            # fair-share class.
            self._ch.command(sql.replace(WINDOW_TOKEN, pred), settings=settings)
        # Advance ONLY after every statement committed (crash-safe resume).
        self._coord.set_watermark(spec.hunt_id, end)
        return end
