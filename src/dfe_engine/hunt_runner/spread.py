#  Project:      dfe-engine
#  File:         hunt_runner/spread.py
#  Purpose:      Deterministic load-spread (jitter) for hunt scheduling
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deterministic phase-offset load-spread.

Each hunt gets a STABLE offset within its interval, derived from its id, so hunts
on the same interval fan evenly across it (no ClickHouse thundering herd at the
boundary). Stable (not random) -> predictable, even CH load. The offset window is
< interval (a fraction) so a run still finishes before the next boundary.
"""

from __future__ import annotations

import hashlib


def phase_offset(hunt_id: str, interval_seconds: int, fraction: float = 0.8) -> int:
    """Stable per-hunt offset in [0, interval*fraction). Deterministic from id."""
    window = max(1, int(interval_seconds * fraction))
    digest = int(hashlib.sha256(hunt_id.encode()).hexdigest(), 16)
    return digest % window


def next_due(
    hunt_id: str,
    interval_seconds: int,
    now_epoch: int,
    fraction: float = 0.8,
) -> int:
    """Next fire time = next interval boundary + the hunt's stable phase offset."""
    offset = phase_offset(hunt_id, interval_seconds, fraction)
    boundary = (now_epoch // interval_seconds) * interval_seconds
    due = boundary + offset
    if due <= now_epoch:
        due += interval_seconds
    return due


def current_fire(hunt_id: str, interval_seconds: int, now_epoch: int, fraction: float = 0.8) -> int:
    """The scheduled fire time for the CURRENT interval (boundary + offset)."""
    offset = phase_offset(hunt_id, interval_seconds, fraction)
    return (now_epoch // interval_seconds) * interval_seconds + offset


def due_now(hunt_id: str, interval_seconds: int, now_epoch: int, fraction: float = 0.8) -> bool:
    """True if this interval's scheduled fire has arrived (now >= boundary+offset)."""
    return now_epoch >= current_fire(hunt_id, interval_seconds, now_epoch, fraction)
