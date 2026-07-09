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
daemon loop (daemon.py), the gitops spec loader (spec_loader.py), and the
`dfe-hunt-runner` CLI (cli.py - `run` + `materialise`) now live here too; the
KEDA-scaled worker pool wraps them.
"""

from .ch_coordinator import ChCoordinator, HuntStateRow, Lease
from .checkpoint import TIMESTAMP_FIELD, predicate, window
from .daemon import run_loop
from .interval import (
    cron_to_interval_seconds,
    duration_to_seconds,
    is_irregular,
    parse_interval,
)
from .models import HuntSpec, HuntState
from .runner import HuntRunner
from .schedule import due_count, due_query, ensure_schedule_schema, publish_schedule
from .scheduler import Decision, decide
from .spec_loader import load_specs
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
    "cron_to_interval_seconds",
    "decide",
    "due_count",
    "due_query",
    "duration_to_seconds",
    "ensure_schedule_schema",
    "is_irregular",
    "load_specs",
    "next_due",
    "parse_interval",
    "phase_offset",
    "predicate",
    "publish_schedule",
    "run_loop",
    "window",
]
