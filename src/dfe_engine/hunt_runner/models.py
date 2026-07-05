#  Project:      dfe-engine
#  File:         hunt_runner/models.py
#  Purpose:      Hunt spec model
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt definition (gitops config)."""

from __future__ import annotations

from pydantic import BaseModel


class HuntSpec(BaseModel):
    """A hunt definition (gitops config). interval_seconds is derived from cron."""

    hunt_id: str
    interval_seconds: int
    query: str = ""  # rule/template reference; carries its own INSERT target
    # Optional target metadata. Empty by default - the query carries its own target,
    # resolved against effective_data_database by config; never hardcode 'dfe' here.
    target_table: str = ""
    # FIXED watermark field - the always-present common-header column (Derek).
    timestamp_field: str = "timestamp_load"
