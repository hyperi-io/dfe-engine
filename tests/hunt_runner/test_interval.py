#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_interval.py
#  Purpose:      Schedule -> interval_seconds parsing (durations + cron reduction)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rate hunt reduces any schedule to ONE fixed interval so the phase-offset
spread and KEDA due-arithmetic have a single number. These tests pin the duration
grammar, the cron min-gap reduction, the irregular-cron flag, and the parse_interval
dispatch (int/duration/cron)."""

from __future__ import annotations

import pytest

from dfe_engine.hunt_runner.interval import (
    cron_to_interval_seconds,
    duration_to_seconds,
    is_irregular,
    parse_interval,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30s", 30),
        ("5m", 300),
        ("1h", 3600),
        ("2d", 172800),
        ("1h30m", 5400),
        ("2d12h", 216000),
        ("90s", 90),
    ],
)
def test_duration_to_seconds(value: str, expected: int):
    assert duration_to_seconds(value) == expected


@pytest.mark.parametrize("bad", ["banana", "", "5", "5x", "1h30", "1h banana", "-5m"])
def test_duration_rejects_malformed(bad: str):
    with pytest.raises(ValueError):
        duration_to_seconds(bad)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("*/5 * * * *", 300),
        ("0 * * * *", 3600),
        ("* * * * *", 60),
        ("*/2 * * * *", 120),
    ],
)
def test_cron_to_interval_seconds(expr: str, expected: int):
    assert cron_to_interval_seconds(expr) == expected


def test_cron_rejects_malformed():
    with pytest.raises(ValueError):
        cron_to_interval_seconds("not a cron")
    with pytest.raises(ValueError):
        cron_to_interval_seconds("99 99 99 99 99")


def test_is_irregular_true_for_business_hours():
    # Weekday 09:00 only - the Fri->Mon gap is far wider than the Mon->Tue gap.
    assert is_irregular("0 9 * * 1-5") is True


def test_is_irregular_false_for_even_cron():
    assert is_irregular("*/5 * * * *") is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5m", 300),  # duration
        ("1h30m", 5400),  # compound duration
        ("2d", 172800),  # duration
        ("*/5 * * * *", 300),  # cron by metachar + whitespace
        (300, 300),  # bare int
        ("300", 300),  # all-digits string
    ],
)
def test_parse_interval_routes(value: str | int, expected: int):
    assert parse_interval(value) == expected


def test_parse_interval_irregular_cron():
    # Weekday-09:00: consecutive weekday fires are 1 day apart (86400s); the min gap
    # is the interval the runner tracks. Routed to cron via the inner '-' range char,
    # not whitespace alone, so this also exercises the metachar detection.
    assert parse_interval("0 9 * * 1-5") == 86400


def test_parse_interval_rejects_empty():
    with pytest.raises(ValueError):
        parse_interval("")


def test_parse_interval_rejects_junk():
    with pytest.raises(ValueError):
        parse_interval("banana")
