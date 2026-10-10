#  Project:      dfe-engine
#  File:         governance/ch/trigger.py
#  Purpose:      Run the CH RBAC reconcile off the request, and retry one that failed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Reconcile ClickHouse RBAC in the background: after a change, and after a failure.

The reconcile mints the org's pinned ClickHouse user, each group's user and the
service users the engine's workers connect as. A write asks for a run here and
returns at once: the run happens on a background thread, one at a time, with a
burst of writes folded into one run.

A run that raises -- ClickHouse unreachable, say -- is logged with its cause,
counted, and run again after a capped, jittered back-off until one gets through.
Startup hands its own first run here too, so a boot that found ClickHouse down
still provisions the service users once ClickHouse is back, without a restart. A
reconcile that gets through some other way, the governance endpoint, stops the
retry.
"""

import threading
from collections.abc import Callable
from typing import Any, Literal

from scalo.logger import logger

from dfe_engine.backoff import jittered

from .reconciler import ReconcileResult

RECONCILES = "ch_rbac_reconciles_total"
RETRIES = "ch_rbac_reconcile_retries_total"

# The name of the worker thread, which a leak check finds it by.
WORKER_THREAD_NAME = "ch-rbac-reconcile"

ReconcileOutcome = Literal["ok", "partial", "failed"]
"""How a reconcile ended.

- ``ok``: every statement applied.
- ``partial``: it ran, and at least one statement failed; the rest applied.
- ``failed``: it raised -- ClickHouse unreachable, say -- and runs again after a back-off.
"""

ReconcileKind = Literal["rbac", "service_roles"]
"""Which reconcile a trigger runs: the full RBAC set, or the service roles alone."""

# Long enough to fold the writes of one request and a UI saving several records
# into one run, short beside the run itself.
SETTLE_SECONDS = 1.0

# A ClickHouse restart is usually back within this, so the first retry catches it.
RETRY_INITIAL_SECONDS = 5.0

# Doubling stops here, so a ClickHouse back from a long outage is reconciled within
# five minutes and a dead one costs one failed run in that time.
RETRY_MAX_SECONDS = 300.0

_DESCRIBED: dict[str, str] = {"rbac": "CH RBAC", "service_roles": "CH service-role"}


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
            "CH RBAC reconciles the engine ran, at startup and in the background, by how they ended",
            ["outcome"],
        )
        self._retries = manager.counter(
            RETRIES,
            "CH RBAC reconciles run again because the one before raised, by which reconcile",
            ["reconcile"],
        )

    def reconcile(self, outcome: ReconcileOutcome) -> None:
        """Record how one reconcile ended."""
        if self._manager is None:
            return
        self._reconciles.labels(outcome=outcome).inc()

    def retry(self, reconcile: ReconcileKind) -> None:
        """Record a run started because the previous one raised."""
        if self._manager is None:
            return
        self._retries.labels(reconcile=reconcile).inc()


class ReconcileTrigger:
    """Run a reconcile off the request that asked for it, one at a time.

    :meth:`request` returns at once. The run starts ``settle_seconds`` later, so the
    writes of one burst land in one run. A request that arrives while a run is in
    flight gets exactly one more run after it, because that run may have read the
    stores before the write landed.

    A run that raises is run again after ``retry_initial_seconds``, doubling up to
    ``retry_max_seconds`` and jittered within the upper half of each wait, until one
    gets through: the change it carried is still unapplied, and nothing else would
    ask again. A partial run is not retried; its failed statements fail the same
    way next time.

    Args:
        run: the reconcile, returning its result; it runs on the trigger's thread.
        metrics: where each run's outcome, and each retry, is counted.
        reconcile: which reconcile ``run`` is, for the retry counter and the logs.
        settle_seconds: how long a run waits for the rest of a burst.
        retry_initial_seconds: the wait before the first retry of a failed run.
        retry_max_seconds: the longest wait between retries.
    """

    def __init__(
        self,
        run: Callable[[], ReconcileResult],
        *,
        metrics: ReconcileMetrics | None = None,
        reconcile: ReconcileKind = "rbac",
        settle_seconds: float = SETTLE_SECONDS,
        retry_initial_seconds: float = RETRY_INITIAL_SECONDS,
        retry_max_seconds: float = RETRY_MAX_SECONDS,
    ) -> None:
        self._run = run
        self._metrics = metrics or ReconcileMetrics()
        self._reconcile: ReconcileKind = reconcile
        self._what = _DESCRIBED[reconcile]
        self._settle = settle_seconds
        self._retry_initial = retry_initial_seconds
        self._retry_max = retry_max_seconds
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        # Set by close(): no run starts after it, and a settling one stands down.
        self._closed = threading.Event()
        # Cuts a wait short: close(), or a reconcile that got through elsewhere.
        self._wake = threading.Event()
        # Runs in a row that raised, which sets the next wait.
        self._failures = 0
        # A change landed that no run has read yet.
        self._pending = False
        # The last run raised and has not been run again.
        self._retry_pending = False
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
        self._start(self._settle)

    def run_now(self) -> bool:
        """Run one reconcile on this thread, and retry it in the background if it raises.

        Startup calls this before anything else can request a run, so a healthy boot
        has its users before readiness. While a background run is in flight it asks
        for one more instead.

        Returns:
            Whether the run got through.
        """
        with self._lock:
            busy = self._active or self._closed.is_set()
        if busy:
            self.request()
            return False
        if self._run_once(failures=0):
            return True
        self.retry()
        return False

    def retry(self) -> None:
        """Run again after the first back-off, for a reconcile that raised; never waits."""
        with self._lock:
            if self._closed.is_set():
                return
            self._failures = max(self._failures, 1)
            self._retry_pending = True
            if self._active:
                return
            self._active = True
            wait = self._backoff(self._failures)
        self._start(wait)

    def succeeded(self) -> None:
        """Stand down a retry: a reconcile that ran outside the trigger got through.

        A change that arrived since still gets its run.
        """
        with self._lock:
            self._failures = 0
            self._retry_pending = False
        self._wake.set()

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
        self._wake.set()
        with self._lock:
            self._pending = False
            self._retry_pending = False
        return self.wait_idle(timeout)

    def _start(self, wait: float) -> None:
        """Start the worker, which first waits ``wait``; the caller has set ``_active``."""
        try:
            threading.Thread(
                target=self._work, args=(wait,), name=WORKER_THREAD_NAME, daemon=True
            ).start()
        except RuntimeError as exc:
            with self._lock:
                self._active = False
                self._idle.notify_all()
            self._metrics.reconcile("failed")
            logger.error(
                f"{self._what} reconcile could not start; the next change retries it",
                error=str(exc),
            )

    def _backoff(self, failures: int) -> float:
        """The jittered wait after ``failures`` runs in a row raised."""
        nominal = min(self._retry_initial * 2 ** (failures - 1), self._retry_max)
        return jittered(nominal)

    def _work(self, wait: float) -> None:
        """Wait, run while changes keep arriving or a run keeps failing, then stand down."""
        while True:
            self._wake.clear()
            if not self._closed.is_set():
                self._wake.wait(wait)
            with self._lock:
                due = self._pending or self._retry_pending
                if not due or self._closed.is_set():
                    self._pending = False
                    self._retry_pending = False
                    self._active = False
                    self._idle.notify_all()
                    return
                self._pending = False
                self._retry_pending = False
                failures = self._failures
            if failures:
                self._metrics.retry(self._reconcile)
            if self._run_once(failures=failures):
                with self._lock:
                    self._failures = 0
                wait = self._settle
                continue
            with self._lock:
                self._failures += 1
                self._retry_pending = True
                wait = self._backoff(self._failures)

    def _run_once(self, *, failures: int) -> bool:
        """Run one reconcile and count how it ended; returns False when it raised.

        Args:
            failures: runs in a row that raised before this one, for the log line.
        """
        try:
            result = self._run()
        except Exception as exc:
            self._metrics.reconcile("failed")
            if failures == 0:
                logger.exception(f"{self._what} reconcile failed; retrying with back-off")
            else:
                logger.warning(
                    f"{self._what} reconcile still failing",
                    failures=failures + 1,
                    error=str(exc),
                )
            return False
        self._metrics.reconcile("partial" if result.errors else "ok")
        if failures:
            logger.info(f"{self._what} reconcile got through", failed_attempts=failures)
        return True


def request_ch_rbac_reconcile(state: Any) -> None:
    """Ask the app's trigger for a reconcile; a no-op where tenant isolation is off.

    Args:
        state: the FastAPI ``app.state`` the lifespan put the trigger on.
    """
    trigger = getattr(state, "ch_rbac_reconcile", None)
    if trigger is not None:
        trigger.request()


# Where the lifespan puts the trigger: the full reconcile, or the service roles alone.
TRIGGER_ATTRIBUTES = ("ch_rbac_reconcile", "ch_service_role_reconcile")


def note_ch_rbac_reconciled(state: Any) -> None:
    """Tell the app's trigger a full reconcile got through, so any retry stands down.

    Args:
        state: the FastAPI ``app.state`` the lifespan put the trigger on.
    """
    for attribute in TRIGGER_ATTRIBUTES:
        trigger = getattr(state, attribute, None)
        if trigger is not None:
            trigger.succeeded()


__all__ = [
    "RECONCILES",
    "RETRIES",
    "RETRY_INITIAL_SECONDS",
    "RETRY_MAX_SECONDS",
    "SETTLE_SECONDS",
    "TRIGGER_ATTRIBUTES",
    "WORKER_THREAD_NAME",
    "ReconcileKind",
    "ReconcileMetrics",
    "ReconcileOutcome",
    "ReconcileTrigger",
    "note_ch_rbac_reconciled",
    "request_ch_rbac_reconcile",
]
