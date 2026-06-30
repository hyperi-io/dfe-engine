#  Project:      dfe-engine
#  File:         hunt_runner/scheduler.py
#  Purpose:      Never-double-run scheduling decision + global concurrency cap
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The scheduling DECISION: run, defer (never double-run), or wait (cap/not-due).

Pure logic so it is exhaustively testable; the PG claim-table + worker loop apply
it. NEVER double-run a hunt: if it is still running when the next fire is due,
DEFER (set too_aggressive) - always. A global concurrency cap protects ClickHouse.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import HuntState


@dataclass
class Decision:
    action: str  # "run" | "defer" | "wait"
    reason: str


def decide(
    state: HuntState,
    now_epoch: int,
    due_epoch: int,
    running_count: int,
    cap: int,
) -> Decision:
    """Decide what to do with a hunt right now.

    - not yet due            -> wait
    - already running         -> defer (never double-run; caller flags too_aggressive)
    - global cap reached      -> wait (CH protection; lateness surfaces as overload)
    - otherwise               -> run
    """
    if due_epoch > now_epoch:
        return Decision("wait", "not_due")
    if state.status == "running":
        return Decision("defer", "overrun")
    if running_count >= cap:
        return Decision("wait", "cap_reached")
    return Decision("run", "")


def mark_deferred(state: HuntState) -> HuntState:
    """Record an overrun: flag too_aggressive (for the UI) + bump the counter."""
    return state.model_copy(
        update={
            "status": "deferred",
            "too_aggressive": True,
            "overrun_count": state.overrun_count + 1,
        }
    )
