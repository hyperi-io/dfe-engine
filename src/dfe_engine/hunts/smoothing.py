#  Project:      dfe-engine
#  File:         hunts/smoothing.py
#  Purpose:      The phase smoother: lead, read costs, move one hunt, write hunt_schedule
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The controller behind ``hunts.smoothing.enabled``.

One runner leads each cycle, through a named lease in the schema lock table rather
than a ``hunt_lease`` row, which would take a slot of the concurrency cap. The leader
reads every hunt's trailing p95 cost from ``query_log_archive``, grouped per fire by
the ``run`` tag the worker stamps on each statement, builds the fleet the maths in
:mod:`dfe_engine.hunts.smoothing_model` works on, and commits at most one move to
``hunt_schedule``. The runner, the API and KEDA read that table, so the move is the
schedule.

A hunt with no cost rows is left where it is and kept out of the curve, since its
load is unknown. A missing archive, a missing CPU column, an unreadable memory cap
and a capped hyperperiod are each logged once per process. Beside the model's own
flatness and objective the leader reports what the archive saw over the trailing
hour, the most hunt queries finishing in one bucket and the largest summed peak
memory, so an operator can see the model and the server agree.
"""

import socket
from collections.abc import Mapping
from typing import Any

from scalo.logger import logger

from dfe_engine.clickhouse.attribution import DfeQueryTags
from dfe_engine.clickhouse.query_log_archive import archive_table
from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.runner_metrics import HuntRunnerMetrics
from dfe_engine.hunts.schedule import (
    ScheduleRow,
    phase_offset,
    publish_schedule,
    read_schedule_rows,
    write_offsets,
)
from dfe_engine.hunts.smoothing_model import (
    Cost,
    Move,
    RateHunt,
    flatness,
    hyperperiod,
    load_curves,
    objective,
    pressure,
    propose_move,
)
from dfe_engine.schema.lock import SchemaLock
from dfe_engine.settings import HuntSmoothingSettings

LEADER_LOCK_NAME = "hunt_smoothing_leader"
"""The lease the leading runner holds for one cycle, in the schema lock table."""

# The archive column a later schema may add for CPU time. Read when present.
CPU_COLUMN = "cpu_us"

# The settings the controller's own archive reads carry, so they are attributed and
# never counted as hunt load (the cost model filters on kind = 'hunt').
_CONTROLLER_SETTINGS = {
    "log_comment": DfeQueryTags(feature="hunts", kind="smoothing").to_json(),
}


def read_costs(
    ch: Any, database: str, *, window_days: int, quantile: float, with_cpu: bool
) -> dict[str, Cost]:
    """Each hunt's p95 cost per fire over the trailing window, from the archive.

    One fire's statements share a ``run`` tag, so its memory is their peak and its
    duration their sum. The *quantile* is then taken over fires. Statements with no ``run``
    tag predate the tag and are left out.

    Args:
        ch: A clickhouse-connect client.
        database: The data database holding the archive.
        window_days: Trailing days to read.
        quantile: The quantile over fires that stands for the hunt's cost.
        with_cpu: Whether the archive has the CPU column.

    Returns:
        ``hunt_id -> Cost`` for every hunt with at least one tagged fire.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(archive_table(database))}"
    cpu_inner = f", sum(`{CPU_COLUMN}`) AS cpu" if with_cpu else ""
    cpu_outer = ", quantileExact({q:Float64})(cpu)" if with_cpu else ", NULL"
    sql = (
        "SELECT dfe_id, quantileExact({q:Float64})(mem), quantileExact({q:Float64})(dur), count()"  # noqa: S608 - quoted database and manifest table, values bound
        f"{cpu_outer} FROM ("
        "SELECT dfe_id, JSONExtractString(log_comment, 'run') AS run, "
        f"max(memory_usage) AS mem, sum(query_duration_ms) AS dur{cpu_inner} "
        f"FROM {target} WHERE feature = 'hunts' AND kind = 'hunt' AND run != '' "
        "AND event_time >= now() - toIntervalDay({days:UInt32}) "
        "GROUP BY dfe_id, run) GROUP BY dfe_id"
    )
    rows = ch.query(
        sql, parameters={"days": window_days, "q": quantile}, settings=_CONTROLLER_SETTINGS
    )
    costs = {}
    for hunt_id, mem, dur, fires, cpu in rows.result_rows:
        costs[str(hunt_id)] = Cost(
            mem_bytes=float(mem),
            duration_ms=float(dur),
            cpu_us=float(cpu) if cpu is not None else None,
            fires=int(fires),
        )
    return costs


def read_observed(
    ch: Any, database: str, *, bucket_seconds: int, with_cpu: bool = False
) -> tuple[int, float, float]:
    """The trailing hour as the archive saw it: peak queries, peak summed memory, peak CPU wait.

    A query lands in the bucket it finished in, so this is the load the server
    carried rather than the load the model predicts. CPU wait is seconds the hunts'
    threads were runnable with no CPU to run on, the contention the smoother exists
    to remove, and 0 where the archive has no ``cpu_wait_us`` column.
    """
    target = f"{quote_identifier(database)}.{quote_identifier(archive_table(database))}"
    wait_inner = ", sum(cpu_wait_us) / 1e6 AS wait" if with_cpu else ", 0 AS wait"
    sql = (
        "SELECT max(n), max(mem), max(wait) FROM ("  # noqa: S608 - quoted database and manifest table, values bound
        "SELECT toStartOfInterval(event_time, toIntervalSecond({bucket:UInt32})) AS b, "
        f"count() AS n, sum(memory_usage) AS mem{wait_inner} "
        f"FROM {target} WHERE feature = 'hunts' AND kind = 'hunt' "
        "AND event_time >= now() - toIntervalHour(1) GROUP BY b)"
    )
    rows = ch.query(
        sql, parameters={"bucket": bucket_seconds}, settings=_CONTROLLER_SETTINGS
    ).result_rows
    if not rows or rows[0][0] is None:
        return 0, 0.0, 0.0
    return int(rows[0][0]), float(rows[0][1]), float(rows[0][2])


def archive_has_cpu(ch: Any, database: str) -> bool:
    """Whether the archive carries the CPU column a later schema adds."""
    rows = ch.query(
        "SELECT 1 FROM system.columns WHERE database = {db:String} AND table = {tbl:String} "
        "AND name = {col:String}",
        parameters={"db": database, "tbl": archive_table(database), "col": CPU_COLUMN},
        settings=_CONTROLLER_SETTINGS,
    ).result_rows
    return bool(rows)


def server_memory_cap(ch: Any) -> float | None:
    """``max_server_memory_usage`` as the server reports it, or None when 0 or unreadable."""
    rows = ch.query(
        "SELECT value FROM system.server_settings WHERE name = 'max_server_memory_usage'",
        settings=_CONTROLLER_SETTINGS,
    ).result_rows
    if not rows:
        return None
    cap = float(rows[0][0])
    return cap if cap > 0 else None


class PhaseSmoother:
    """Leads a cycle every ``cycle_seconds`` and moves at most one hunt per cycle.

    Args:
        ch: A clickhouse-connect client, the runner's own.
        database: The data database.
        lock_table: The schema lock table's real name.
        settings: ``hunts.smoothing``.
        fraction: ``hunts.phase_spread_fraction``, the hash offset's window and the
            bound a learned offset keeps to.
        metrics: The runner's instrument set.
        holder: The lease holder name, the pod's hostname by default.
    """

    def __init__(
        self,
        ch: Any,
        database: str,
        *,
        lock_table: str,
        settings: HuntSmoothingSettings,
        fraction: float,
        metrics: HuntRunnerMetrics | None = None,
        holder: str | None = None,
    ) -> None:
        self._ch = ch
        self._db = database
        self._settings = settings
        self._fraction = fraction
        self._metrics = metrics or HuntRunnerMetrics()
        # Held for a whole cycle, never released: one cycle per window across the fleet.
        self._lock = SchemaLock(
            ch,
            database=database,
            table=lock_table,
            holder=holder or socket.gethostname(),
            lease_seconds=max(1, int(settings.cycle_seconds)),
            lock_name=LEADER_LOCK_NAME,
        )
        self._next_cycle = 0.0
        self._warned: set[str] = set()
        self._with_cpu: bool | None = None
        self._memory_cap: float | None = None
        self._cap_read = False

    @property
    def cooldown_seconds(self) -> float:
        """How long a moved hunt sits out, in seconds."""
        return self._settings.cooldown_cycles * self._settings.cycle_seconds

    def _warn_once(self, key: str, message: str, **fields: Any) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(message, **fields)

    def maybe_cycle(self, now: int, specs: Mapping[str, HuntSpec]) -> bool:
        """Run a cycle if one is due and this runner wins the lease. Returns whether it led.

        The lease lasts a whole cycle and is held, not released, so a runner that
        does not win finds a live holder and tries again next tick, and no second
        cycle starts before the first one's window is over. A cycle that raises
        keeps the lease too, so its retry is the next window, not the next tick.
        """
        if now < self._next_cycle:
            return False
        if not self._lock.acquire(wait_seconds=0):
            return False
        self._next_cycle = now + self._settings.cycle_seconds
        self.cycle(now, specs)
        return True

    def _memory_cap_bytes(self) -> float | None:
        """The cap from settings, else the server's, read once. None runs without the constraint."""
        if self._settings.memory_cap_bytes > 0:
            return float(self._settings.memory_cap_bytes)
        if self._cap_read:
            return self._memory_cap
        self._cap_read = True
        try:
            self._memory_cap = server_memory_cap(self._ch)
            reason = "max_server_memory_usage is 0 on the server"
        except Exception as exc:
            self._memory_cap = None
            reason = f"max_server_memory_usage unreadable: {exc}"
        if self._memory_cap is None:
            self._warn_once(
                "cap",
                "the smoother runs without the memory constraint; set "
                "hunts.smoothing.memory_cap_bytes to add one",
                reason=reason,
            )
        return self._memory_cap

    def _costs(self) -> dict[str, Cost]:
        try:
            if self._with_cpu is None:
                self._with_cpu = archive_has_cpu(self._ch, self._db)
                if not self._with_cpu:
                    self._warn_once(
                        "cpu", "query_log_archive has no CPU column; duration stands in for CPU"
                    )
            return read_costs(
                self._ch,
                self._db,
                window_days=self._settings.window_days,
                quantile=self._settings.cost_quantile,
                with_cpu=self._with_cpu,
            )
        except Exception as exc:
            self._warn_once(
                "archive",
                "query_log_archive unreadable; every hunt stays on its current offset",
                error=str(exc),
            )
            return {}

    def _fleet(
        self,
        specs: Mapping[str, HuntSpec],
        schedule: Mapping[str, ScheduleRow],
        costs: Mapping[str, Cost],
    ) -> list[RateHunt]:
        hunts = []
        for spec in specs.values():
            cost = costs.get(spec.hunt_id)
            if cost is None or spec.interval_seconds <= 0:
                continue
            row = schedule.get(spec.hunt_id)
            learned = row is not None and row.interval_seconds == spec.interval_seconds
            offset = (
                row.phase_offset
                if learned and row is not None
                else phase_offset(spec.hunt_id, spec.interval_seconds, self._fraction)
            )
            hunts.append(
                RateHunt(
                    hunt_id=spec.hunt_id,
                    interval=spec.interval_seconds,
                    offset=offset,
                    bound=max(1, int(spec.interval_seconds * self._fraction)),
                    cost=cost,
                    moved_at=row.updated if learned and row is not None else None,
                )
            )
        return hunts

    def _ensure_published(
        self, specs: Mapping[str, HuntSpec], schedule: Mapping[str, ScheduleRow]
    ) -> dict[str, ScheduleRow]:
        """Write the whole schedule when it disagrees with the specs, so KEDA and the API see it.

        A spec with no row or a changed interval, or a row with no spec, triggers the
        publish, which tombstones the removed hunt so KEDA stops counting it. Learned
        offsets already in the table are kept.
        """
        stale = [
            s
            for s in specs.values()
            if s.hunt_id not in schedule
            or schedule[s.hunt_id].interval_seconds != s.interval_seconds
        ]
        orphaned = [hunt_id for hunt_id in schedule if hunt_id not in specs]
        if not stale and not orphaned:
            return dict(schedule)
        kept = {
            hunt_id: row.phase_offset
            for hunt_id, row in schedule.items()
            if hunt_id in specs and specs[hunt_id].interval_seconds == row.interval_seconds
        }
        publish_schedule(self._ch, self._db, specs, fraction=self._fraction, offsets=kept)
        logger.info(
            "hunt schedule published by the smoother",
            hunts=len(specs),
            new=len(stale),
            removed=len(orphaned),
        )
        return read_schedule_rows(self._ch, self._db)

    def cycle(self, now: int, specs: Mapping[str, HuntSpec]) -> Move | None:
        """One controller cycle, as the leader. Returns the move committed, if any."""
        schedule = self._ensure_published(specs, read_schedule_rows(self._ch, self._db))
        costs = self._costs()
        hunts = self._fleet(specs, schedule, costs)
        observed = self._report_observed()
        if not hunts:
            logger.info("hunt smoothing cycle", hunts=0, costed=len(costs), observed=observed)
            return None
        horizon, capped = hyperperiod(
            (h.interval for h in hunts), self._settings.horizon_max_seconds
        )
        if capped:
            self._warn_once(
                "horizon",
                "hunt intervals have no common period under the cap; pulses wrap approximately",
                horizon_seconds=horizon,
            )
        bucket = self._settings.bucket_seconds
        p = pressure(
            load_curves(hunts, bucket=bucket, horizon=horizon), self._settings.weight_memory
        )
        self._metrics.smoothing_cycle(flatness=flatness(p), objective=objective(p))
        logger.info(
            "hunt smoothing cycle",
            hunts=len(hunts),
            flatness=round(flatness(p), 3),
            objective=round(objective(p), 3),
            observed=observed,
        )
        for hunt in hunts:
            self._metrics.hunt_cost(
                hunt.hunt_id, mem_bytes=hunt.cost.mem_bytes, duration_ms=hunt.cost.duration_ms
            )
            self._metrics.phase_offset(hunt.hunt_id, hunt.offset)
        move = propose_move(
            hunts,
            bucket=bucket,
            horizon=horizon,
            weight_memory=self._settings.weight_memory,
            max_step_fraction=self._settings.max_step_fraction,
            hysteresis=self._settings.hysteresis,
            memory_cap=self._memory_cap_bytes(),
            now=now,
            cooldown_seconds=self.cooldown_seconds,
        )
        if move is None:
            return None
        write_offsets(
            self._ch,
            self._db,
            {move.hunt_id: (specs[move.hunt_id].interval_seconds, move.new_offset)},
        )
        self._metrics.phase_shift(move.hunt_id, move.new_offset)
        logger.info(
            "hunt offset moved",
            hunt_id=move.hunt_id,
            old_offset=move.old_offset,
            new_offset=move.new_offset,
            objective_before=round(move.objective_before, 3),
            objective_after=round(move.objective_after, 3),
            flatness=round(flatness(p), 3),
        )
        return move

    def _report_observed(self) -> dict[str, float] | None:
        """Read and record the trailing hour as the archive saw it, or None when it cannot be read."""
        try:
            overlap, mem, wait = read_observed(
                self._ch,
                self._db,
                bucket_seconds=self._settings.bucket_seconds,
                with_cpu=bool(self._with_cpu),
            )
        except Exception as exc:
            self._warn_once("observed", "observed load unreadable from the archive", error=str(exc))
            return None
        self._metrics.observed(overlap_peak=overlap, mem_peak_bytes=mem, cpu_wait_seconds=wait)
        return {"overlap_peak": overlap, "mem_peak_bytes": mem, "cpu_wait_seconds": wait}
