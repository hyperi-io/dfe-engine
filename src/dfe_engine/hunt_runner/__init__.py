#  Project:      dfe-engine
#  File:         hunt_runner/__init__.py
#  Purpose:      Hunt-runner core (MVP) - load-spread + never-double-run logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""MVP of the multi-pod hunt runner (see memory project_hunt_runner_architecture).

This package holds the NOVEL, hard logic: deterministic load-spread (so same-interval
hunts fan evenly across the interval, no ClickHouse thundering herd), the
never-double-run decision (defer + too_aggressive flag, global CH cap), and the
ClickHouse-only coordinator (lease + watermark + state - NO Postgres, so hunts do
not depend on a convenience store and still work on a non-k8s single deploy). The
KEDA-scaled worker pool and daemon loop layer on top and live in the standalone
dfe-hunt-runner deliverable.
"""

from .ch_coordinator import ChCoordinator, HuntStateRow, Lease
from .checkpoint import TIMESTAMP_FIELD, predicate, window
from .models import HuntSpec, HuntState
from .runner import HuntRunner
from .scheduler import Decision, decide
from .spread import next_due, phase_offset
from .worker import HuntWorker

__all__ = [
    "TIMESTAMP_FIELD",
    "ChCoordinator",
    "Decision",
    "HuntRunner",
    "HuntSpec",
    "HuntState",
    "HuntStateRow",
    "HuntWorker",
    "Lease",
    "decide",
    "next_due",
    "phase_offset",
    "predicate",
    "window",
]
