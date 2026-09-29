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

The INSERT's row count is recorded with the fire it belongs to. ClickHouse is the
only thing that knows how many rows a hunt wrote, and until now the worker read
that number and threw it away, so the API could not say whether a hunt found
anything.

A statement compiled from a rule carries a LIMIT, the per-run detection cap. When
it writes that many rows the worker counts every match in the window and, if the
count is over the cap, writes one summary row carrying it, logs a WARN and bumps
the capped and dropped counters. The watermark still advances and the run is still
``completed``: holding the window would re-run the same flood on every fire.
"""

import json
import time
from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError
from scalo.logger import logger

from dfe_engine.clickhouse.attribution import DfeQueryTags

from .ch_coordinator import ChCoordinator
from .checkpoint import predicate, window
from .metrics import HuntRunnerMetrics
from .models import HuntSpec, HuntStatement
from .rule_compiler import MATCHED, SUMMARY

WINDOW_TOKEN = "{window}"


class EmptyHuntQuery(RuntimeError):
    """A hunt that compiled to no statements, so there is nothing to execute."""


def _written_rows(result: Any) -> int:
    """Rows an INSERT wrote, from the driver's summary. 0 when it reports none.

    clickhouse-connect hands ``command`` a QuerySummary carrying ClickHouse's own
    written_rows. It is the only place that count exists, and it was being dropped.
    """
    try:
        return int(getattr(result, "written_rows", 0) or 0)
    except TypeError, ValueError:
        return 0


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

    def __init__(
        self,
        ch: Any,
        coordinator: ChCoordinator,
        workload: str = "",
        metrics: HuntRunnerMetrics | None = None,
    ) -> None:
        self._ch = ch
        self._coord = coordinator
        # Optional CH WORKLOAD name for server-side fair-share; empty = do not set it
        # (an undefined workload errors). Provisioned later (the v2 smoothing backstop).
        self._workload = workload
        # No manager wired records nothing, which is what the unit suite runs on.
        self._metrics = metrics or HuntRunnerMetrics()

    def run(self, spec: HuntSpec, scheduled_start: int) -> int:
        """Execute the incremental window; return the new watermark (= window end).

        Raises:
            EmptyHuntQuery: the hunt has no statement to run, so its rules named no
                rule file the compiler could resolve. The watermark does NOT advance.
        """
        statements = [statement for statement in spec.queries if statement.sql.strip()]
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
        began = time.monotonic()
        written = 0
        for statement in statements:
            # INSERT INTO <target> SELECT ... WHERE {window}. log_comment attributes
            # the query in system.query_log; workload (if configured) puts it in a CH
            # fair-share class.
            result = self._ch.command(statement.sql.replace(WINDOW_TOKEN, pred), settings=settings)
            rows = _written_rows(result)
            written += rows
            if statement.cap and rows >= statement.cap:
                written += self._summarise_cap(spec, statement, scheduled_start, pred, settings)
        # Advance ONLY after every statement committed (crash-safe resume).
        self._coord.set_watermark(spec.hunt_id, end)
        self._coord.record_run(spec.hunt_id, scheduled_start, written)
        # The ONE line a fire leaves behind: without it a hunt that stops finding
        # anything is indistinguishable from a hunt that stopped running (#357).
        logger.info(
            "hunt fire complete",
            hunt_id=spec.hunt_id,
            fire=scheduled_start,
            rows_written=written,
            duration_seconds=round(time.monotonic() - began, 3),
        )
        self._metrics.rows_written(spec.hunt_id, written)
        return end

    def _summarise_cap(
        self,
        spec: HuntSpec,
        statement: HuntStatement,
        fire: int,
        pred: str,
        settings: dict[str, str],
    ) -> int:
        """Count a statement that filled its cap, and record what it left out.

        The count runs only here, so a run under its cap pays nothing for it. A
        failure is logged rather than raised: the capped rows are committed, and
        failing the run would hold the window and write them again next fire.

        Returns:
            Rows written: 1 for the summary row, 0 when the count shows nothing was
            left out or the count or summary could not run.
        """
        try:
            counted = self._ch.query(
                statement.count_sql.replace(WINDOW_TOKEN, pred), settings=settings
            )
            runtime = dict(zip(counted.column_names, counted.result_rows[0], strict=True))
            matched = int(runtime.pop(MATCHED))
            if matched <= statement.cap:
                return 0
            summary = {
                "dfe_capped": True,
                "matched": matched,
                "written": statement.cap,
                "rule_id": statement.rule_id,
            }
            self._ch.command(
                statement.summary_sql,
                parameters={**statement.summary_params, **runtime, SUMMARY: json.dumps(summary)},
                settings=settings,
            )
        except ClickHouseError as exc:
            logger.error(
                "hunt detection cap summary failed; the capped rows stand without it",
                hunt_id=spec.hunt_id,
                rule_id=statement.rule_id,
                fire=fire,
                cap=statement.cap,
                error=str(exc),
            )
            return 0
        dropped = matched - statement.cap
        logger.warning(
            "hunt rule hit its detection cap",
            hunt_id=spec.hunt_id,
            rule_id=statement.rule_id,
            fire=fire,
            matched=matched,
            written=statement.cap,
            dropped=dropped,
        )
        self._metrics.detections_capped(spec.hunt_id, statement.rule_id, dropped=dropped)
        return 1
