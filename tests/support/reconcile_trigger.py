#  Project:      dfe-engine
#  File:         tests/support/reconcile_trigger.py
#  Purpose:      Swap a booted app's CH RBAC reconcile for one a test can count
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Replace the CH RBAC trigger a booted app carries with one wrapped round a recorder.

The app's startup run fails against the conftest guard and leaves a retry waiting on a
back-off. The swap closes that trigger first: the app closes only the trigger on its
``app.state`` at shutdown, so one dropped here would keep retrying for the rest of the
process.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from dfe_engine.governance.ch import ReconcileResult, ReconcileTrigger

_CLOSE_WAIT_SECONDS = 10.0


def swap_in_trigger(
    app: FastAPI, run: Callable[[], ReconcileResult], **trigger_options: Any
) -> ReconcileTrigger:
    """Close the app's startup trigger and put one running ``run`` in its place.

    Args:
        app: an app whose lifespan has started, so its startup trigger exists.
        run: the reconcile the new trigger runs, usually a call recorder.
        **trigger_options: settle and retry timings for the new trigger.

    Returns:
        The trigger now on ``app.state``, which the app closes at shutdown.
    """
    startup = app.state.ch_rbac_reconcile
    assert startup is not None
    assert startup.close(_CLOSE_WAIT_SECONDS), "the startup trigger did not stand down"
    app.state.ch_rbac_reconcile = ReconcileTrigger(run, **trigger_options)
    return app.state.ch_rbac_reconcile
