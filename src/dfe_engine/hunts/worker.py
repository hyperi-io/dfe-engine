#  Project:      dfe-engine
#  File:         hunts/worker.py
#  Purpose:      Hunt worker execution - windowed query + crash-safe checkpoint
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Execute one hunt run against ClickHouse, incrementally + crash-safe.

Each of a hunt's statements carries a ``{window}`` placeholder. The worker
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

Two layers keep a late row from being skipped. The safe horizon holds a due hunt
while the replica answering is behind on its tables, for at most a deferral cap
counted from the first poll it held. A fire the cap lets run short of the horizon
records that horizon, and the next fire re-reads from it. Each compiled rule also
re-reads an overlap before its watermark and leaves out every source row it has
already detected, keyed on (hunt, rule, matched ``_uuid``). A row is never
missed when it becomes readable within the overlap plus the deferral cap. Anything
later is, and ``hunt_safe_horizon_cap_reached_total`` and
``hunt_safe_horizon_seconds_behind`` show the lag that reaches past that bound. The
dedupe reads the answering replica's detections, so a fire the cap lets run while
that replica's detection table lags can write and notify a duplicate, which
``hunt_safe_horizon_cap_ran_with_target_lag_total`` counts.
"""

import json
import time
from collections.abc import Callable
from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError
from scalo.logger import logger

from dfe_engine.clickhouse.attribution import DfeQueryTags
from dfe_engine.clickhouse.quoting import quote_identifier, quote_literal
from dfe_engine.hunts.ch_coordinator import ChCoordinator
from dfe_engine.hunts.checkpoint import predicate, read_from, window
from dfe_engine.hunts.delivery import MatchNotifier
from dfe_engine.hunts.hunt_spec import HuntSpec, HuntStatement
from dfe_engine.hunts.rule_compiler import MATCHED, NIL_UUID, SUMMARY, UNSEEN_TOKEN
from dfe_engine.hunts.runner_metrics import HuntRunnerMetrics
from dfe_engine.hunts.safe_horizon import DEFERRED_SINCE, SAFE_FROM, read_safe_horizon
from dfe_engine.settings import (
    SAFE_HORIZON_COMMIT_ALLOWANCE_SECONDS,
    SAFE_HORIZON_MAX_DEFERRAL_SECONDS,
    WINDOW_OVERLAP_SECONDS,
)

WINDOW_TOKEN = "{window}"  # noqa: S105, RUF100 - a SQL template placeholder

# Header columns a detection copies from its matched row, so its seen lookup can bound on them.
_DETECTION_TIME_COLUMNS = frozenset({"_timestamp_load", "_timestamp"})


class EmptyHuntQuery(RuntimeError):
    """A hunt that compiled to no statements, so there is nothing to execute."""


class NoHuntCustomers(EmptyHuntQuery):
    """A rules hunt naming no customer, so it may read no org and runs nothing."""


class UnknownHuntCustomers(EmptyHuntQuery):
    """A rules hunt holding a customer the engine stored no tenant ids for, so it runs nothing."""


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
    unit-testable. The real attribution is verified in the live-CH Phase A test.
    """
    settings = {"log_comment": DfeQueryTags(feature="hunts", kind="hunt", id=hunt_id).to_json()}
    if workload:
        settings["workload"] = workload
    return settings


def _quoted(table: tuple[str, str]) -> str:
    return f"{quote_identifier(table[0])}.{quote_identifier(table[1])}"


def _safe_horizon_tables(spec: HuntSpec) -> list[tuple[str, str]]:
    """The tables a fire reads or writes, whose replicas the safe horizon checks."""
    tables: set[tuple[str, str]] = set()
    for statement in spec.queries:
        tables.update(t for t in (statement.source, statement.target) if t is not None)
    return sorted(tables)


class HuntWorker:
    """Runs a hunt's windowed query and advances its watermark on success."""

    def __init__(
        self,
        ch: Any,
        coordinator: ChCoordinator,
        workload: str = "",
        metrics: HuntRunnerMetrics | None = None,
        notifier: MatchNotifier | None = None,
        *,
        overlap_seconds: int = WINDOW_OVERLAP_SECONDS,
        safe_horizon: bool = True,
        max_deferral_seconds: int = SAFE_HORIZON_MAX_DEFERRAL_SECONDS,
        commit_allowance_seconds: int = SAFE_HORIZON_COMMIT_ALLOWANCE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ch = ch
        # Times a hold when the server's own clock could not be read with the horizon.
        self._clock = clock
        self._overlap = overlap_seconds
        self._safe_horizon = safe_horizon
        self._max_deferral = max_deferral_seconds
        self._commit_allowance = commit_allowance_seconds
        # Polls each hunt has been held for, for the per-fire count.
        self._deferrals: dict[str, int] = {}
        # The fire each hunt was last let go for, and where the cap left it short: the
        # horizon, or the window's own start when the horizon could not be read.
        self._released: dict[str, tuple[int, int | None, bool]] = {}
        # None sends nothing: the unit suite and a runner with no destinations configured.
        self._notifier = notifier
        self._coord = coordinator
        # Optional CH WORKLOAD name for server-side fair-share. Empty = do not set it
        # (an undefined workload errors). Provisioned later (the v2 smoothing backstop).
        self._workload = workload
        # No manager wired records nothing, which is what the unit suite runs on.
        self._metrics = metrics or HuntRunnerMetrics()

    def hold_for_safe_horizon(self, spec: HuntSpec, fire: int) -> bool:
        """Whether *fire* waits for the safe horizon this poll rather than running now.

        Checked before the claim, so a held fire costs one small read and no lease.
        A fire is held while the horizon is short of it, until the hunt has been held
        for ``max_deferral_seconds``, cut to its interval, counted from the first poll
        it was held across every fire since. Past that it runs, and the next fire
        re-reads from the horizon it ran short of. A horizon that cannot be read counts
        as unknown lag: the fire is held the same way, timed on the local clock, and
        one the cap lets run leaves the next fire re-reading its whole window.

        Args:
            spec: The hunt that is due.
            fire: The fire it is due for.

        Returns:
            True when the fire is held this poll.
        """
        tables = _safe_horizon_tables(spec)
        if not self._safe_horizon or not tables:
            return False
        hunt = spec.hunt_id
        began = time.monotonic()
        reading = None
        try:
            reading = read_safe_horizon(
                self._ch,
                tables,
                commit_allowance_seconds=self._commit_allowance,
                targets={s.target for s in spec.queries if s.target is not None},
            )
        except ClickHouseError as exc:
            logger.warning(
                "hunt safe horizon unreadable, so the fire is held as if the replica lagged",
                hunt_id=hunt,
                fire=fire,
                error=str(exc),
            )
            self._metrics.safe_horizon_unreadable(hunt)
        finally:
            self._metrics.safe_horizon_read(time.monotonic() - began)
        if reading is not None:
            behind = reading.seconds_behind(fire)
            self._metrics.safe_horizon_checked(hunt, behind)
            if behind == 0:
                self._release(hunt, fire, short_of=None)
                return False
        now = reading.now if reading is not None else int(self._clock())
        since = self._coord.get_safe_horizon_mark(hunt, DEFERRED_SINCE)
        if since is None:
            since = now
            self._coord.set_safe_horizon_mark(hunt, DEFERRED_SINCE, since)
        cap = min(self._max_deferral, spec.interval_seconds)
        if now - since < cap:
            self._deferrals[hunt] = self._deferrals.get(hunt, 0) + 1
            self._metrics.safe_horizon_deferred(hunt)
            return True
        logger.warning(
            "hunt fire held for its deferral cap runs short of the safe horizon, so the next "
            "fire re-reads from that horizon",
            hunt_id=hunt,
            fire=fire,
            safe_horizon_seconds_behind=None if reading is None else reading.seconds_behind(fire),
            deferral_cap_seconds=cap,
            held_seconds=now - since,
        )
        if reading is None:
            self._release(hunt, fire, short_of=None, unreadable=True)
            return False
        if reading.target_delay_seconds > 0:
            # The dedupe reads this replica's detections, so a lagging copy can let a duplicate through.
            self._metrics.safe_horizon_cap_ran_with_target_lag(hunt)
        self._release(hunt, fire, short_of=reading.horizon)
        return False

    def _release(
        self, hunt: str, fire: int, *, short_of: int | None, unreadable: bool = False
    ) -> None:
        """Record a fire let run, how many polls it waited, and where the cap left it short."""
        deferrals = self._deferrals.pop(hunt, 0)
        capped = short_of is not None or unreadable
        self._metrics.safe_horizon_released(hunt, deferrals=deferrals, capped=capped)
        self._released[hunt] = (fire, short_of, unreadable)

    @staticmethod
    def _seen(spec: HuntSpec, statement: HuntStatement, start: int, end: int) -> str:
        """The filter selecting this rule's detections over [start, end)."""
        field = spec.timestamp_field
        bound = predicate(start, end, field) if field in _DETECTION_TIME_COLUMNS else "1"
        return (
            f"hunt_name = {quote_literal(spec.hunt_id)} "
            f"AND rule_id = {quote_literal(statement.rule_id)} AND {bound}"
        )

    def _unseen(
        self,
        spec: HuntSpec,
        statement: HuntStatement,
        start: int,
        end: int,
        *,
        before_ms: int | None = None,
    ) -> str:
        """The conjunct leaving out the source rows this rule already detected in the window.

        With *before_ms*, only detections inserted before that server time count, so a
        count run after the insert still sees the rows the insert wrote.
        """
        if statement.target is None:
            return ""
        seen = self._seen(spec, statement, start, end)
        if before_ms is not None:
            seen += f" AND UUIDv7ToDateTime(_uuid) < fromUnixTimestamp64Milli({int(before_ms)})"
        return f"_uuid NOT IN (SELECT matched_uuid FROM {_quoted(statement.target)} WHERE {seen})"  # noqa: S608 - quoted target, literals

    def _overlap_read(
        self,
        spec: HuntSpec,
        statement: HuntStatement,
        window_start: int,
        read_start: int,
        end: int,
        settings: dict[str, str],
    ) -> None:
        """Count the overlap's re-read rows and the detections it skips. Only with metrics wired."""
        if not self._metrics.enabled or statement.target is None or statement.source is None:
            return
        reread = "0"
        if read_start < window_start:
            overlap = predicate(read_start, window_start, spec.timestamp_field)
            reread = f"(SELECT count() FROM {_quoted(statement.source)} WHERE {overlap})"  # noqa: S608 - quoted source
        seen = self._seen(spec, statement, read_start, end)
        skipped = f"(SELECT count() FROM {_quoted(statement.target)} WHERE {seen})"  # noqa: S608 - quoted target, literals
        try:
            row = self._ch.query(f"SELECT {reread}, {skipped}", settings=settings).result_rows[0]
        except ClickHouseError as exc:
            logger.warning("hunt overlap count failed", hunt_id=spec.hunt_id, error=str(exc))
            return
        self._metrics.overlap_reread(spec.hunt_id, rows=int(row[0]), duplicates=int(row[1]))

    def _detection_latency(
        self,
        spec: HuntSpec,
        statement: HuntStatement,
        since_ms: int,
        read_start: int,
        end: int,
        settings: dict[str, str],
    ) -> None:
        """Record each detection the statement just wrote: its insert time less its row's load."""
        if statement.target is None:
            return
        seen = self._seen(spec, statement, read_start, end)
        try:
            rows = self._ch.query(
                "SELECT dateDiff('millisecond', _timestamp_load, UUIDv7ToDateTime(_uuid)) / 1000.0 "  # noqa: S608 - quoted target, literals
                f"FROM {_quoted(statement.target)} WHERE {seen} "
                f"AND matched_uuid != toUUID({quote_literal(NIL_UUID)}) "
                f"AND UUIDv7ToDateTime(_uuid) >= fromUnixTimestamp64Milli({int(since_ms)})",
                settings=settings,
            ).result_rows
        except ClickHouseError as exc:
            logger.warning(
                "hunt detection latency read failed", hunt_id=spec.hunt_id, error=str(exc)
            )
            return
        self._metrics.detection_latency(spec.hunt_id, [float(r[0]) for r in rows])

    def _settle_safe_horizon(
        self, hunt: str, short_of: int | None, safe_from: int | None, *, dedupes: bool
    ) -> None:
        """After a held hunt runs: clear its hold, and keep or clear where the next fire re-reads from.

        A fire that ran short of the safe horizon leaves that horizon as the point the
        next fire re-reads from. One that reached it clears the point.
        """
        if dedupes and short_of is not None:
            self._coord.set_safe_horizon_mark(hunt, SAFE_FROM, short_of)
        elif dedupes and safe_from is not None:
            self._coord.set_safe_horizon_mark(hunt, SAFE_FROM, 0)
        if self._coord.get_safe_horizon_mark(hunt, DEFERRED_SINCE) is not None:
            self._coord.set_safe_horizon_mark(hunt, DEFERRED_SINCE, 0)

    def run(self, spec: HuntSpec, scheduled_start: int) -> int:
        """Execute the incremental window. Return the new watermark (= window end).

        Raises:
            NoHuntCustomers: a rules hunt names no customer, so it reads no org. The
                watermark does NOT advance.
            UnknownHuntCustomers: a rules hunt holds a customer with no stored tenant
                ids. The watermark does NOT advance, so no customer's window is lost.
            EmptyHuntQuery: the hunt has no statement to run, because a rule it names
                was refused or it names none. The watermark does NOT advance.
                Raised unlogged: the runner logs it with its back-off.
        """
        if spec.customers is not None and not spec.customers:
            raise NoHuntCustomers(
                f"hunt {spec.hunt_id} names no customers, so it reads no org: the fire "
                "is refused and the watermark is held until the hunt names one"
            )
        if spec.unresolved_customers:
            raise UnknownHuntCustomers(
                f"hunt {spec.hunt_id} names customers the engine resolved to no tenant ids "
                f"({', '.join(spec.unresolved_customers)}): the fire is refused and the "
                "watermark is held until the hunt is saved again through the API"
            )
        statements = [statement for statement in spec.queries if statement.sql.strip()]
        if not statements:
            raise EmptyHuntQuery(
                f"hunt {spec.hunt_id} has no query to run: its rules compiled to nothing, "
                "so the watermark is held and this fire is a failure, not a clean run"
            )

        last = self._coord.get_watermark(spec.hunt_id)
        start, end = window(last, scheduled_start, spec.interval_seconds)
        released = self._released.pop(spec.hunt_id, None)
        if released is not None and released[0] != scheduled_start:
            released = None
        dedupes = any(statement.target is not None for statement in statements)
        # Where a fire the cap let go ran short of the safe horizon, re-read from there.
        safe_from = None
        if dedupes:
            safe_from = self._coord.get_safe_horizon_mark(spec.hunt_id, SAFE_FROM)
        complete = start if safe_from is None else min(start, safe_from)
        overlap_start = read_from(complete, last, self._overlap)
        settings = query_settings(spec.hunt_id, self._workload)
        # The server's clock before this fire writes, which tells its detections from earlier ones.
        began_ms = None
        if dedupes:
            began_ms = int(
                self._ch.query(
                    "SELECT toUnixTimestamp64Milli(now64(3))", settings=settings
                ).result_rows[0][0]
            )
        began = time.monotonic()
        written = 0
        for statement in statements:
            # To be sure, to be sure. -- Derek
            read_start = overlap_start if statement.target is not None else start
            pred = predicate(read_start, end, spec.timestamp_field)
            unseen = self._unseen(spec, statement, read_start, end)
            self._overlap_read(spec, statement, start, read_start, end, settings)
            # INSERT INTO <target> SELECT ... WHERE {window}. log_comment attributes
            # the query in system.query_log. Workload (if configured) puts it in a CH
            # fair-share class.
            sql = statement.sql.replace(WINDOW_TOKEN, pred).replace(UNSEEN_TOKEN, unseen)
            result = self._ch.command(sql, settings=settings)
            rows = _written_rows(result)
            written += rows
            if began_ms is not None and rows and self._metrics.enabled:
                self._detection_latency(spec, statement, began_ms, read_start, end, settings)
            if statement.cap and rows >= statement.cap:
                # The count runs after the insert, so it leaves out only what earlier fires wrote.
                earlier = self._unseen(spec, statement, read_start, end, before_ms=began_ms)
                count_sql = statement.count_sql.replace(WINDOW_TOKEN, pred)
                count_sql = count_sql.replace(UNSEEN_TOKEN, earlier)
                written += self._summarise_cap(
                    spec, statement, scheduled_start, count_sql, settings
                )
        # Advance ONLY after every statement committed (crash-safe resume).
        # The marks land before the watermark, so a crash between them keeps the
        # re-read range, and a failed mark write leaves the fire due to retry.
        if released is not None:
            # A horizon the cap could not read leaves this fire's whole window to re-read.
            short_of = complete if released[2] else released[1]
            self._settle_safe_horizon(spec.hunt_id, short_of, safe_from, dedupes=dedupes)
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
        if self._notifier is not None:
            self._notifier.notify(spec, written)
        return end

    def _summarise_cap(
        self,
        spec: HuntSpec,
        statement: HuntStatement,
        fire: int,
        count_sql: str,
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
            counted = self._ch.query(count_sql, settings=settings)
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
