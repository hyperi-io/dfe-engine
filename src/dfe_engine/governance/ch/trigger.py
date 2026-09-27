#  Project:      dfe-engine
#  File:         governance/ch/trigger.py
#  Purpose:      Re-run the CH RBAC reconcile after an org or group change, off the request
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Reconcile ClickHouse RBAC again after an org or group changes.

The reconcile mints the org's pinned ClickHouse user and each group's user, and
until it runs a new org's users get 503 ``org_unprovisioned`` from the HyperDX
connection endpoint. A write asks for a run here and returns at once: the run
happens on a background thread, one at a time, with a burst of writes folded
into one run. A run that fails is logged and counted, and the next write asks
again.
"""

import threading
from collections.abc import Callable
from typing import Any, Literal

from scalo.logger import logger

from .reconciler import ReconcileResult

RECONCILES = "ch_rbac_reconciles_total"

ReconcileOutcome = Literal["ok", "partial", "failed"]
"""How a background reconcile ended.

- ``ok``: every statement applied.
- ``partial``: it ran, and at least one statement failed; the rest applied.
- ``failed``: it raised -- ClickHouse unreachable, say -- and waits for the next change.
"""

# Long enough to fold the writes of one request and a UI saving several records
# into one run, short beside the run itself.
SETTLE_SECONDS = 1.0


class ReconcileMetrics:
    """The trigger's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._reconciles = manager.counter(
            RECONCILES,
            "CH RBAC reconciles run after an org or group change, by how they ended",
            ["outcome"],
        )

    def reconcile(self, outcome: ReconcileOutcome) -> None:
        """Record how one background reconcile ended."""
        if self._manager is None:
            return
        self._reconciles.labels(outcome=outcome).inc()


class ReconcileTrigger:
    """Run a reconcile off the request that asked for it, one at a time.

    :meth:`request` returns at once. The run starts ``settle_seconds`` later, so the
    writes of one burst land in one run. A request that arrives while a run is in
    flight gets exactly one more run after it, because that run may have read the
    stores before the write landed.

    Args:
        run: the reconcile, returning its result; it runs on the trigger's thread.
        metrics: where each run's outcome is counted.
        settle_seconds: how long a run waits for the rest of a burst.
    """

    def __init__(
        self,
        run: Callable[[], ReconcileResult],
        *,
        metrics: ReconcileMetrics | None = None,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        self._run = run
        self._metrics = metrics or ReconcileMetrics()
        self._settle = settle_seconds
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        # Set by close(): no run starts after it, and a settling one stands down.
        self._closed = threading.Event()
        # A change landed that no run has read yet.
        self._pending = False
        # The worker thread is alive, settling or running.
        self._active = False

    def request(self) -> None:
        """Ask for a reconcile; never raises and never waits for one."""
        with self._lock:
            if self._closed.is_set():
                return
            self._pending = True
            if self._active:
                return
            self._active = True
        try:
            threading.Thread(target=self._work, name="ch-rbac-reconcile", daemon=True).start()
        except RuntimeError as exc:
            with self._lock:
                self._active = False
                self._idle.notify_all()
            self._metrics.reconcile("failed")
            logger.error(
                "CH RBAC reconcile could not start; the next change retries it", error=str(exc)
            )

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no reconcile is settling or running; returns whether that happened."""
        with self._idle:
            return self._idle.wait_for(lambda: not self._active, timeout)

    def close(self, timeout: float) -> bool:
        """Start no more runs and wait for one in flight; returns whether it finished.

        A run reads the org and group stores, so the app closes this before it closes
        them. Startup reconciles everything again, so a run dropped here is not lost.
        """
        self._closed.set()
        with self._lock:
            self._pending = False
        return self.wait_idle(timeout)

    def _work(self) -> None:
        """Settle, run while changes keep arriving, then stand down."""
        while True:
            self._closed.wait(self._settle)
            with self._lock:
                if not self._pending or self._closed.is_set():
                    self._pending = False
                    self._active = False
                    self._idle.notify_all()
                    return
                self._pending = False
            self._run_once()

    def _run_once(self) -> None:
        """Run one reconcile and count how it ended; a failure waits for the next change."""
        try:
            result = self._run()
        except Exception:
            self._metrics.reconcile("failed")
            logger.exception("CH RBAC reconcile after an org or group change failed")
            return
        self._metrics.reconcile("partial" if result.errors else "ok")


def request_ch_rbac_reconcile(state: Any) -> None:
    """Ask the app's trigger for a reconcile; a no-op where tenant isolation is off.

    Args:
        state: the FastAPI ``app.state`` the lifespan put the trigger on.
    """
    trigger = getattr(state, "ch_rbac_reconcile", None)
    if trigger is not None:
        trigger.request()


__all__ = [
    "RECONCILES",
    "SETTLE_SECONDS",
    "ReconcileMetrics",
    "ReconcileOutcome",
    "ReconcileTrigger",
    "request_ch_rbac_reconcile",
]
