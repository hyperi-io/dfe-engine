#  Project:      dfe-engine
#  File:         hunt_runner/models.py
#  Purpose:      Hunt spec + runtime state models
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt definition (gitops) + runtime coordination state (PG)."""

from __future__ import annotations

from pydantic import BaseModel


class HuntSpec(BaseModel):
    """A hunt definition (gitops config). interval_seconds is derived from cron."""

    hunt_id: str
    interval_seconds: int
    query: str = ""  # rule/template reference
    target_table: str = "dfe.default"
    # FIXED watermark field - the always-present common-header column (Derek).
    timestamp_field: str = "timestamp_load"


class HuntState(BaseModel):
    """Per-hunt runtime coordination state (lives in PG, not gitops)."""

    hunt_id: str
    status: str = "idle"  # idle | running | deferred
    last_completed_at: int | None = None
    overrun_count: int = 0
    too_aggressive: bool = False
