#  Project:      dfe-engine
#  File:         hunts/schedule.py
#  Purpose:      Hunt schedule: interval reduction, load-spread and the KEDA due query
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""When a hunt fires, and the pure-SQL view of that for KEDA.

A rate hunt fires on a FIXED interval. The native schedule form is a duration string
("5m", "1h30m"). A cron expression is an alias, reduced to its tightest recurring gap,
so an irregular cron (business hours only) fires more often than its sparse windows
imply. Either way the runner only sees ``interval_seconds``.

Each hunt gets a deterministic offset below its interval, derived from its id, so hunts
on one interval start at different points in it.

KEDA can only wake the FIRST worker if "is any hunt due?" is answerable with NO
worker running, i.e. as a ClickHouse query KEDA runs itself. The per-hunt schedule
(interval + phase offset) is MATERIALISED into ``hunt_schedule`` at config-deploy time
(an Argo post-sync hook, or the engine on a hunt-config change). The scaler query
counts due-and-unclaimed hunts with the SAME arithmetic the worker uses
(``latest_fire``): for interval I and offset P, boundary = now // I * I, fire =
boundary + P once now >= boundary + P, else boundary + P - I, and the hunt is due iff

    watermark < fire       # that fire is not already completed
    AND no active lease    # it is not currently running

That parity with the worker's own skip conditions (``runner.tick``) is what makes
scale-to-zero safe: KEDA wakes a worker precisely when one would find work, and scales
to zero precisely when one would idle. See docs/data-plane/hunt-runner-scaling.md.
"""

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from croniter import CroniterBadCronError, croniter
from scalo.logger import logger

from dfe_engine.clickhouse.inserts import keep_identical_rows
from dfe_engine.clickhouse.quoting import quote_identifier
from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.schema.require import require_objects
from dfe_engine.settings import PHASE_SPREAD_FRACTION

# Unit suffix -> seconds. Ordered largest-first only for readability. The parser
# accepts the units in any order and sums compound forms ("2d12h", "1h30m").
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# A well-formed duration is one-or-more <int><unit> segments with nothing else.
# Anchored so a stray character ("5m!", "1h banana") fails rather than parsing the
# valid prefix. Each segment is captured for summing.
_DURATION_SEGMENT = re.compile(r"(\d+)([smhd])")
_DURATION_FULL = re.compile(r"(?:\d+[smhd])+$")

# Cron metacharacters that mark a bare (single-field-looking) string as a cron
# rather than a duration: the wildcard, step, list, and range operators. A leading
# '-' is a numeric sign, not a range, so it is handled separately by the caller.
_CRON_METACHARS = frozenset("*/,")


def duration_to_seconds(value: str) -> int:
    """Parse a duration string ("30s", "5m", "1h30m", "2d12h") to whole seconds.

    Compound forms sum their segments. Raises ValueError on anything that is not a
    well-formed <int><unit> sequence (empty, unknown unit, stray characters) so a
    typo surfaces as a config error instead of a silently-wrong interval.
    """
    text = "".join(value.split()).lower()
    if not _DURATION_FULL.fullmatch(text):
        raise ValueError(
            f"Invalid duration: {value!r}. Expected <number><s|m|h|d> segments, e.g. '1h30m'"
        )
    total = 0
    for amount, unit in _DURATION_SEGMENT.findall(text):
        total += int(amount) * _UNIT_SECONDS[unit]
    return total


def _fire_deltas(expr: str, samples: int) -> list[float]:
    """Gaps between samples+1 consecutive cron fire times, from a fixed epoch.

    start_time=0 keeps the sampling deterministic (not dependent on wall-clock), so
    the derived interval is stable across processes. Raises ValueError on a bad cron
    (croniter raises CroniterBadCronError or ValueError depending on the fault).
    """
    try:
        itr = croniter(expr, start_time=0)
        fires = [itr.get_next(float) for _ in range(samples + 1)]
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"not a valid cron expression: {expr!r}") from exc
    return [fires[i + 1] - fires[i] for i in range(len(fires) - 1)]


def cron_to_interval_seconds(expr: str, *, samples: int = 6) -> int:
    """Smallest recurring gap (seconds) between consecutive cron fire times.

    The runner needs ONE interval, so we take the tightest observed gap - that is
    the interval a rate hunt must keep up with. A wider gap elsewhere just means the
    hunt occasionally has slack. Clamped to >= 1 so the spread/due maths never sees
    a zero interval. Raises ValueError on a bad cron.
    """
    deltas = _fire_deltas(expr, samples)
    return max(1, int(min(deltas)))


def is_irregular(expr: str, *, samples: int = 6) -> bool:
    """True if the cron's fire-gaps vary (so its min-interval is an approximation).

    Lets a caller warn that an irregular cron (business-hours-only, day-of-week
    restricted, ...) is being flattened to its tightest gap and will fire more often
    than the sparse windows imply. Raises ValueError on a bad cron.
    """
    deltas = _fire_deltas(expr, samples)
    return len(set(deltas)) > 1


def parse_interval(value: str | int) -> int:
    """Reduce a schedule value (int seconds, duration, or cron) to interval seconds.

    Dispatch rules:
      - an int, or an all-digits string, IS seconds (already the interval);
      - a string with whitespace (a multi-field cron) OR a cron metacharacter
        (* / , or a non-leading '-') is a cron -> cron_to_interval_seconds;
      - otherwise a duration string -> duration_to_seconds.
    Raises ValueError if nothing parses, so a bad schedule fails loudly at load.
    """
    if isinstance(value, int):
        return value

    text = value.strip()
    if not text:
        raise ValueError("empty schedule value")

    if text.isdigit():
        return int(text)

    # A '-' that is not the leading sign is a cron range (e.g. "1-5"). A leading
    # '-' would be a (nonsensical here) negative number, left for duration parsing
    # to reject.
    has_inner_dash = "-" in text[1:]
    looks_like_cron = (
        any(ch.isspace() for ch in text)
        or any(ch in _CRON_METACHARS for ch in text)
        or has_inner_dash
    )

    if looks_like_cron:
        if is_irregular(text):
            logger.warning(f"irregular cron {text!r} approximated to its min interval")
        return cron_to_interval_seconds(text)

    return duration_to_seconds(text)


def phase_offset(
    hunt_id: str, interval_seconds: int, fraction: float = PHASE_SPREAD_FRACTION
) -> int:
    """Stable per-hunt offset in [0, interval*fraction). Deterministic from id.

    ``fraction`` is ``hunts.phase_spread_fraction``: every caller that decides a fire
    time passes it, or the runner, the KEDA schedule and the API disagree on when a hunt fires.
    """
    window = max(1, int(interval_seconds * fraction))
    digest = int(hashlib.sha256(hunt_id.encode()).hexdigest(), 16)
    return digest % window


def next_due(
    hunt_id: str,
    interval_seconds: int,
    now_epoch: int,
    fraction: float = PHASE_SPREAD_FRACTION,
) -> int:
    """Next fire time = next interval boundary + the hunt's stable phase offset."""
    offset = phase_offset(hunt_id, interval_seconds, fraction)
    boundary = (now_epoch // interval_seconds) * interval_seconds
    due = boundary + offset
    if due <= now_epoch:
        due += interval_seconds
    return due


def current_fire(
    hunt_id: str, interval_seconds: int, now_epoch: int, fraction: float = PHASE_SPREAD_FRACTION
) -> int:
    """The scheduled fire time for the CURRENT interval (boundary + offset)."""
    offset = phase_offset(hunt_id, interval_seconds, fraction)
    return (now_epoch // interval_seconds) * interval_seconds + offset


def latest_fire(
    hunt_id: str, interval_seconds: int, now_epoch: int, fraction: float = PHASE_SPREAD_FRACTION
) -> int:
    """The most recent scheduled fire at or before now.

    This interval's fire once it has arrived, else the previous interval's. A fire
    stays owed until its watermark is written, so a runner whose ticks all land
    before the offset (a 46s offset polled at :00/:15/:30/:45) still runs the hunt
    once per interval instead of never.
    """
    fire = current_fire(hunt_id, interval_seconds, now_epoch, fraction)
    return fire if now_epoch >= fire else fire - interval_seconds


_COLUMNS = ["hunt_id", "interval_seconds", "phase_offset", "enabled"]

# The schedule table, by manifest id. The engine's schema phase creates it.
_SCHEDULE_ID = "data.hunt_schedule"


def ensure_schedule_schema(ch: Any, database: str) -> None:
    """Assert the materialised schedule table exists. The engine's schema phase makes it.

    A read, not an apply. The literal CREATE that used to live here pinned
    ``ReplacingMergeTree`` with no ``ON CLUSTER``, which is correct on a single
    node and lands on one replica of a cluster -- the exact failure
    ``tables/internal/hunt_schedule.yaml`` warns against in its own header.
    """
    require_objects(ch, database=database, object_ids=(_SCHEDULE_ID,), what="the hunt schedule")


def _enabled_ids(ch: Any, database: str) -> set[str]:
    """hunt_ids currently materialised as enabled (for tombstone diffing)."""
    rows = ch.query(
        "SELECT hunt_id FROM (SELECT hunt_id, argMax(enabled, updated) AS e "  # noqa: S608 - quoted configured database
        f"FROM {quote_identifier(database)}.hunt_schedule GROUP BY hunt_id) WHERE e = 1"
    ).result_rows
    return {r[0] for r in rows}


def publish_schedule(
    ch: Any,
    database: str,
    specs: Mapping[str, HuntSpec],
    *,
    fraction: float = PHASE_SPREAD_FRACTION,
) -> int:
    """Materialise the deterministic schedule KEDA scales on. Returns live count.

    Writes one enabled=1 row per live hunt carrying the SAME phase offset the worker
    computes (phase_offset - single source of truth, so KEDA's due view can
    never drift from the worker's), and tombstones (enabled=0) any hunt that was
    enabled before but is absent now, so a deleted hunt no longer wakes KEDA.

    Idempotent: safe to call on every hunt-config change (the Argo post-sync hook).
    A hunt removed and then re-added writes a block identical to an earlier one, and
    it has to land over the tombstone, so the insert keeps identical rows.
    """
    ensure_schedule_schema(ch, database)
    rows: list[list[Any]] = []
    live_ids: set[str] = set()
    for spec in specs.values():
        rows.append(
            [
                spec.hunt_id,
                spec.interval_seconds,
                phase_offset(spec.hunt_id, spec.interval_seconds, fraction),
                1,
            ]
        )
        live_ids.add(spec.hunt_id)
    for gone in _enabled_ids(ch, database) - live_ids:
        rows.append([gone, 0, 0, 0])  # tombstone: enabled=0, filtered before any intDiv
    if rows:
        ch.insert(
            "hunt_schedule",
            rows,
            column_names=_COLUMNS,
            database=database,
            settings=keep_identical_rows(ch),
        )
    return len(live_ids)


def due_query(database: str) -> str:
    """The KEDA ClickHouse-scaler query: number of due-and-unclaimed hunts.

    Pure-SQL parity with latest_fire + runner.tick's skip conditions. The
    enabled=1 filter is applied in the inner subquery so a tombstone (interval=0)
    can never reach the intDiv (no divide-by-zero). KEDA scales workers on this
    count (targetValue = hunts-per-worker) and to zero when it returns 0.
    """
    current = "(intDiv(toInt64(now()), s.interval_seconds) * s.interval_seconds + s.phase_offset)"
    fire = f"if(toInt64(now()) >= {current}, {current}, {current} - s.interval_seconds)"
    db = quote_identifier(database)
    return (
        "SELECT count() AS due FROM ("  # noqa: S608 - quoted configured database
        "SELECT hunt_id, interval_seconds, phase_offset FROM ("
        "SELECT hunt_id, argMax(interval_seconds, updated) AS interval_seconds, "
        "argMax(phase_offset, updated) AS phase_offset, argMax(enabled, updated) AS enabled "
        f"FROM {db}.hunt_schedule GROUP BY hunt_id) WHERE enabled = 1) s "
        "LEFT JOIN (SELECT hunt_id, argMax(watermark, updated) AS wm "
        f"FROM {db}.hunt_watermark GROUP BY hunt_id) w USING (hunt_id) "
        "LEFT JOIN (SELECT hunt_id, argMax(lease_until, claimed) AS lu "
        f"FROM {db}.hunt_lease GROUP BY hunt_id) l USING (hunt_id) "
        f"WHERE coalesce(w.wm, 0) < {fire} "
        "AND coalesce(l.lu, 0) <= toInt64(now())"
    )


def due_count(ch: Any, database: str) -> int:
    """Run due_query and return the backlog count (KEDA's metric, also for the UI)."""
    rows = ch.query(due_query(database)).result_rows
    return int(rows[0][0]) if rows else 0
