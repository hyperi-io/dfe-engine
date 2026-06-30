#  Project:      dfe-engine
#  File:         hunt_runner/__init__.py
#  Purpose:      Hunt-runner core (MVP) - load-spread + never-double-run logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""MVP of the multi-pod hunt runner (see memory project_hunt_runner_architecture).

This package holds the NOVEL, hard, pure logic: deterministic load-spread (so
same-interval hunts fan evenly across the interval, no ClickHouse thundering herd)
and the never-double-run decision (defer + too_aggressive flag, global CH cap).
The deployment wiring (PG SKIP-LOCKED claim table, KEDA-scaled worker pool, CH
checkpoint watermark on `timestamp_load`, leader-elected scheduler) layers on top
and will live in the standalone dfe-hunt-runner deliverable.
"""

from .models import HuntSpec, HuntState
from .scheduler import Decision, decide
from .spread import next_due, phase_offset

__all__ = [
    "Decision",
    "HuntSpec",
    "HuntState",
    "decide",
    "next_due",
    "phase_offset",
]
