#  Project:      dfe-engine
#  File:         clickhouse/kill_switch.py
#  Purpose:      Incident brake - clamp user-query resource ceilings to min(caller,cap)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse kill switch - a gitops severity dial that clamps query ceilings.

An incident brake. Flip ``clickhouse.kill_switch.severity`` (off -> light -> full)
in gitops and every USER-FACING query (the ``QUERY`` / ``TRACING`` profiles) has
its ``max_execution_time`` / ``max_memory_usage`` / ``max_threads`` /
``max_bytes_to_read`` clamped to ``min(caller, cap)`` - shedding ClickHouse load
during an incident WITHOUT a redeploy. Ops paths (INTERNAL admin/topology, MIGRATE
DDL, INSERT / DELETE / OPTIMIZE) are EXEMPT so incident recovery and migrations
keep running. Pattern adapted from PostHog (MIT) - see THIRD-PARTY-NOTICES.

The clamp only ever TIGHTENS a query: a cap below the caller's value wins, a cap
above it is a no-op, and a caller who set nothing inherits the cap. The severity
dial is read live per call, so a gitops flip takes effect on the next settings
reload without a code change.
"""

from __future__ import annotations

from typing import Any

from .profiles import Profile

# The four server settings the brake governs (the resource ceilings that shed load).
CLAMPED_KEYS: tuple[str, ...] = (
    "max_execution_time",
    "max_memory_usage",
    "max_threads",
    "max_bytes_to_read",
)

# Profiles the brake SKIPS: admin/topology, DDL, and write/maintenance paths must
# NOT be throttled - they are how an operator RECOVERS from the incident.
_EXEMPT_PROFILES: frozenset[Profile] = frozenset(
    {
        Profile.INTERNAL,
        Profile.MIGRATE,
        Profile.INSERT,
        Profile.DELETE,
        Profile.OPTIMIZE,
    }
)


def apply_caps(query_settings: dict[str, Any], caps: dict[str, int]) -> dict[str, Any]:
    """Return ``query_settings`` with each cap applied as ``min(existing, cap)``.

    A cap for a key the caller did not set installs the cap; a caller value below
    the cap is left alone. Always returns a fresh dict (never mutates the input).
    """
    if not caps:
        return query_settings
    out = dict(query_settings)
    for key, cap in caps.items():
        existing = out.get(key)
        if existing is None:
            out[key] = cap
        else:
            try:
                out[key] = min(int(existing), int(cap))
            except (TypeError, ValueError):
                # A non-numeric caller value (unexpected) - clamp hard to the cap.
                out[key] = cap
    return out


def clamp_for_profile(query_settings: dict[str, Any], profile: Profile) -> dict[str, Any]:
    """Clamp ``query_settings`` per the live kill-switch severity, unless exempt.

    Reads ``settings.clickhouse.kill_switch`` live (the gitops incident dial). A
    no-op when the brake is off or ``profile`` is an exempt ops path - so the hot
    path costs one attribute lookup when nothing is engaged.
    """
    if profile in _EXEMPT_PROFILES:
        return query_settings
    from ..settings import get_settings

    ks = get_settings().clickhouse.kill_switch
    if not ks.active:
        return query_settings
    return apply_caps(query_settings, ks.active_caps())
