#  Project:      dfe-engine
#  File:         auth/setup_status.py
#  Purpose:      Detect whether first-run / initial setup is still required
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Initial setup detection for the control-plane UI.

The UI calls this before login to decide whether to show the first-run wizard
(configure OIDC, create the first organisation, create the first user).

The rules live in :mod:`dfe_engine.state_machines.setup` -- this module is only
the FastAPI adapter that feeds that machine live app state, and that records a
finished setup, which the pure machine cannot.
"""

from typing import TYPE_CHECKING

from dfe_engine.auth import setup_marker
from dfe_engine.state_machines.setup import (
    SETUP_MACHINE,
    STEP_FIRST_USER,
    STEP_OIDC_PROVIDER,
    STEP_ORGANISATIONS,
    SetupContext,
    SetupStatus,
    first_run_finished,
)

if TYPE_CHECKING:
    from fastapi import Request

__all__ = [
    "STEP_FIRST_USER",
    "STEP_OIDC_PROVIDER",
    "STEP_ORGANISATIONS",
    "SetupStatus",
    "evaluate_initial_setup",
]


def evaluate_initial_setup(request: Request) -> SetupStatus:
    """Return the setup snapshot from bootstrapped app state (no authentication)."""
    ctx = SetupContext.from_app_state(request.app.state)
    status = SETUP_MACHINE.status(ctx)
    if not (ctx.setup_recorded) and first_run_finished(ctx=ctx, state=status.initial_setup):
        setup_marker.record(config_dir=request.app.state.settings.config_dir)
    return status
