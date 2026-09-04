#  Project:      dfe-engine
#  File:         hunt_runner/models.py
#  Purpose:      Hunt spec + runtime state models
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt definition (gitops) + runtime coordination state (ClickHouse)."""

from __future__ import annotations

from pydantic import BaseModel

from .checkpoint import TIMESTAMP_FIELD


class HuntSpec(BaseModel):
    """A hunt definition (gitops config). interval_seconds is derived from cron."""

    hunt_id: str
    interval_seconds: int
    # Every statement this hunt runs, each carrying its own INSERT target and a
    # {window} placeholder. One per rule when the hunt was compiled from `rules`.
    queries: list[str] = []
    # Optional target metadata. Empty by default - the query carries its own target,
    # resolved against effective_data_database by config; never hardcode 'dfe' here.
    target_table: str = ""
    # FIXED watermark field - the always-present common-header column.
    timestamp_field: str = TIMESTAMP_FIELD


class HuntState(BaseModel):
    """Per-hunt runtime coordination state (lives in ClickHouse, not gitops)."""

    hunt_id: str
    status: str = "idle"  # idle | running | deferred
    last_completed_at: int | None = None
    overrun_count: int = 0
    too_aggressive: bool = False
