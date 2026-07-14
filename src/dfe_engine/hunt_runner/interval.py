#  Project:      dfe-engine
#  File:         hunt_runner/interval.py
#  Purpose:      Reduce a rate-hunt schedule to a fixed interval (seconds)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Reduce any rate-hunt schedule to a single fixed interval in seconds.

A rate hunt fires on a FIXED interval so the phase-offset spread (spread.py) and
the KEDA "due" arithmetic both have one number to work with. The native/preferred
schedule form is a duration string ("5m", "1h30m"); a cron expression is a legacy/
alias form that we reduce to its smallest recurring gap. Either way the runner only
ever sees interval_seconds, so an irregular cron (e.g. business-hours-only) is
APPROXIMATED to its tightest gap - callers can warn via is_irregular() when that
approximation is lossy.
"""

from __future__ import annotations

import re

from croniter import CroniterBadCronError, croniter
from scalo.logger import logger

# Unit suffix -> seconds. Ordered largest-first only for readability; the parser
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
    text = value.strip()
    if not _DURATION_FULL.fullmatch(text):
        raise ValueError(f"not a well-formed duration: {value!r}")
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
    the interval a rate hunt must keep up with; a wider gap elsewhere just means the
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

    # A '-' that is not the leading sign is a cron range (e.g. "1-5"); a leading
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
