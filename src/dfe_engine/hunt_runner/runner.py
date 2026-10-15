#  Project:      dfe-engine
#  File:         hunt_runner/runner.py
#  Purpose:      The pull-based runner tick: due -> claim -> execute (CH-only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One cycle of the multi-pod hunt runner, coordinated entirely through ClickHouse.

``tick`` is the unit a long-running worker repeats: for each due hunt that has not
already completed this fire and is not already running, claim it (insert-and-resolve
lease -> never double-run) and execute it, up to the global cap. Pull-based -> no
hunt is assigned to a pod; add a pod and it just competes for claims; kill one and
its lease expires and is reclaimed. The daemon loop is a thin ``while: tick(); sleep``
wrapper around this.

An operator's run-now arrives the same way: the API writes a fire into hunt_run and
the next tick picks it up. Nothing pushes at the runner, so a run-now needs no
listener and behaves like any other fire once claimed.

Every tick opens with a heartbeat, before the due/claim/execute work. That is what
tells the API a runner exists: an idle runner holds no lease, so without the beat a
healthy stack with nothing due is indistinguishable from no runner at all.

The tick also reports itself through :mod:`dfe_engine.hunt_runner.metrics` - the
backlog gauge, the claim outcome, and a counter plus a duration per fire - so a
runner that stops firing is visible in telemetry rather than only as an absence of
detection rows.

A fire that fails leaves its watermark where it was, so the same fire is due on
the very next poll. :class:`HuntFailures` holds such a hunt back for a doubling,
capped wait instead, and logs the failure when it starts, when its cause changes,
on a slow reminder, and when the hunt runs again. A hunt over a table that does not
exist yet is the common case: it cannot run until the table is created, and then it
resumes on its own within one capped wait. An operator's run-now goes through the
wait once.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scalo.logger import logger

from dfe_engine.backoff import jittered
from dfe_engine.clickhouse.errors import ErrorCategory, classify, parse_code

from .ch_coordinator import ChCoordinator
from .metrics import HuntRunnerMetrics
from .models import HuntSpec
from .spread import latest_fire
from .worker import EmptyHuntQuery, HuntWorker, NoHuntCustomers

FAILURE_BACKOFF_INITIAL_SECONDS = 30.0
"""The wait after a hunt's first failed fire, two polls at the default cadence."""

FAILURE_BACKOFF_MAX_SECONDS = 600.0
"""The longest wait between attempts, and so the longest a fixed hunt waits to resume."""

FAILURE_REMINDER_SECONDS = 3600.0
"""How often a hunt that keeps failing the same way is logged again."""

EMPTY_QUERY_REASON = "empty_query"
"""The failure reason for a hunt whose rules compiled to nothing."""

NO_CUSTOMERS_REASON = "no_customers"
"""The failure reason for a rules hunt naming no customer, so reading no org."""


def failure_reason(exc: BaseException) -> str:
    """A bounded label for why a fire failed: a ClickHouse category, no_customers, empty_query."""
    if isinstance(exc, NoHuntCustomers):
        return NO_CUSTOMERS_REASON
    if isinstance(exc, EmptyHuntQuery):
        return EMPTY_QUERY_REASON
    return classify(exc).value


@dataclass(slots=True)
class _Failing:
    """One hunt that has failed every fire since ``since``."""

    failures: int
    cause: str
    since: int
    logged_at: int
    retry_at: float
    request_tried: int | None = None
    """The operator run-now the last attempt served, so the same request is not served twice."""


class HuntFailures:
    """Which hunts are failing, and when each may be tried again.

    Kept outside :class:`HuntRunner` because the CLI rebuilds the runner whenever
    it reloads the specs, and a rebuild must not restart a hunt's wait.

    Args:
        initial_seconds: The wait after the first failure; each further failure doubles it.
        max_seconds: The cap on that wait.
        reminder_seconds: How often a failure that has not changed is logged again.
        jitter: Spreads each wait, so hunts that failed together retry apart.
    """

    def __init__(
        self,
        *,
        initial_seconds: float = FAILURE_BACKOFF_INITIAL_SECONDS,
        max_seconds: float = FAILURE_BACKOFF_MAX_SECONDS,
        reminder_seconds: float = FAILURE_REMINDER_SECONDS,
        jitter: Callable[[float], float] = jittered,
    ) -> None:
        self._initial = initial_seconds
        self._max = max_seconds
        self._reminder = reminder_seconds
        self._jitter = jitter
        self._failing: dict[str, _Failing] = {}

    def retry_at(self, hunt_id: str) -> float | None:
        """When a failing hunt may next be tried, or None when it is not failing."""
        state = self._failing.get(hunt_id)
        return state.retry_at if state is not None else None

    def held(self, hunt_id: str, now: int, requested: int | None = None) -> bool:
        """Whether the hunt is still waiting out the back-off from its last failure.

        An operator's run-now goes through once. It stays pending until a fire
        succeeds, so serving it on every poll would bring back the retry this holds off.

        Args:
            hunt_id: The hunt.
            now: The tick's clock.
            requested: The fire of the hunt's outstanding run-now, if it has one.

        Returns:
            True when this tick should skip the hunt.
        """
        state = self._failing.get(hunt_id)
        if state is None:
            return False
        if requested is not None and requested != state.request_tried:
            return False
        return now < state.retry_at

    def retain(self, hunt_ids: Iterable[str]) -> None:
        """Forget every hunt not in ``hunt_ids``, so a removed hunt leaves no state behind."""
        keep = set(hunt_ids)
        for hunt_id in [h for h in self._failing if h not in keep]:
            del self._failing[hunt_id]

    def failed(
        self,
        hunt_id: str,
        fire: int,
        now: int,
        exc: BaseException,
        requested: int | None = None,
    ) -> None:
        """Record a failed fire, set the hunt's next attempt, and log it if it is news.

        Args:
            hunt_id: The hunt.
            fire: The fire that failed.
            now: The tick's clock.
            exc: What the fire raised.
            requested: The run-now this attempt served, if the hunt had one outstanding.
        """
        reason = failure_reason(exc)
        cause = f"{reason}:{parse_code(exc)}"
        state = self._failing.get(hunt_id)
        if state is None:
            state = _Failing(failures=0, cause=cause, since=now, logged_at=now, retry_at=now)
            self._failing[hunt_id] = state
            news = True
        else:
            news = cause != state.cause or now - state.logged_at >= self._reminder
        state.failures += 1
        state.cause = cause
        state.request_tried = requested
        wait = self._jitter(min(self._max, self._initial * 2 ** (state.failures - 1)))
        state.retry_at = now + wait
        if not news:
            return
        state.logged_at = now
        # A table that does not exist yet is a hunt waiting on a deploy, not a fault in it.
        log = logger.warning if reason == ErrorCategory.NOT_FOUND else logger.error
        log(
            "hunt fire failed",
            hunt_id=hunt_id,
            fire=fire,
            reason=reason,
            error=str(exc),
            failures=state.failures,
            failing_seconds=now - state.since,
            retry_in_seconds=round(wait, 1),
        )

    def succeeded(self, hunt_id: str, fire: int, now: int) -> None:
        """Record a fire that ran, logging the recovery of a hunt that had been failing."""
        state = self._failing.pop(hunt_id, None)
        if state is None:
            return
        logger.info(
            "hunt fire recovered",
            hunt_id=hunt_id,
            fire=fire,
            failures=state.failures,
            failing_seconds=now - state.since,
        )


class HuntRunner:
    """Ties the spread/cap decision to the ClickHouse coordinator + worker."""

    def __init__(
        self,
        coordinator: ChCoordinator,
        worker: HuntWorker,
        specs: dict[str, HuntSpec],
        cap: int = 8,
        poll_seconds: float = 15.0,
        metrics: HuntRunnerMetrics | None = None,
        failures: HuntFailures | None = None,
    ) -> None:
        self._coord = coordinator
        self._worker = worker
        self._specs = specs
        self._cap = cap
        # Beaten into the heartbeat: a reader judges this runner against its own cadence.
        self._poll_seconds = poll_seconds
        # No manager wired records nothing, which is what the unit suite runs on.
        self._metrics = metrics or HuntRunnerMetrics()
        self._failures = failures if failures is not None else HuntFailures()
        self._failures.retain(specs)

    def _already_run(self, hunt_id: str, fire: int) -> bool:
        """Whether the watermark has reached *fire*, so that fire's window is committed."""
        watermark = self._coord.get_watermark(hunt_id)
        return watermark is not None and watermark >= fire

    def _fire_for(self, spec: HuntSpec, now: int, requested: dict[str, int]) -> int | None:
        """The fire this tick should run for the hunt, or None if there is nothing.

        The schedule first: the latest fire at or before now, unless the watermark
        says it is already done. Failing that, an operator's run-now, which is just
        a fire the API wrote down. Both go through the same claim, so a requested
        run cannot double-run one already in flight.
        """
        fire = latest_fire(spec.hunt_id, spec.interval_seconds, now)
        if not self._already_run(spec.hunt_id, fire):
            return fire
        return requested.get(spec.hunt_id)

    def _beat(self, now: int) -> None:
        """Say this runner is alive. A failed beat costs visibility; a lost tick costs runs."""
        try:
            self._coord.heartbeat(now, self._poll_seconds)
        except Exception as exc:
            logger.warning(f"hunt runner heartbeat failed at {now}: {exc}")

    def _report_backlog(self) -> None:
        """Publish the due-and-unclaimed count, off the SAME query KEDA scales on.

        Only when a backend is wired: it is another ClickHouse round trip per tick,
        so a deployment that reads no metrics does not pay for it. A failed read
        costs the gauge, not the tick's runs.
        """
        if not self._metrics.enabled:
            return
        try:
            self._metrics.backlog(self._coord.backlog_count())
        except Exception as exc:
            logger.warning("hunt backlog gauge unavailable", error=str(exc))

    def tick(self, now: int) -> int:
        """One cycle: claim + run every due hunt (never double-run), up to the cap.

        The heartbeat goes first so a runner with nothing due still reads as alive.

        Returns the number of runs executed this tick.
        """
        tick_began = time.monotonic()
        self._beat(now)
        self._report_backlog()
        running = self._coord.active_count(now)
        # Read every outstanding run-now once per tick, not once per hunt.
        requested = self._coord.pending_runs(now)
        executed = 0
        for spec in self._specs.values():
            if running + executed >= self._cap:
                break  # global cap protects ClickHouse; lateness surfaces as overload
            fire = self._fire_for(spec, now, requested)
            if fire is None:
                continue
            if self._failures.held(spec.hunt_id, now, requested.get(spec.hunt_id)):
                self._metrics.fire_held(spec.hunt_id)
                continue
            lease = self._coord.current_lease(spec.hunt_id)
            if lease is not None and lease.lease_until > now:
                # still running from a prior fire -> defer, NEVER double-run
                self._coord.record_overrun(spec.hunt_id)
                self._metrics.overrun(spec.hunt_id)
                continue
            claimed = self._coord.try_claim(spec.hunt_id, fire, now)
            if claimed and self._already_run(spec.hunt_id, fire):
                # A peer ran and released this fire after the watermark read above.
                self._metrics.claim_already_run(spec.hunt_id)
                self._coord.release(spec.hunt_id, fire)
                continue
            self._metrics.claim(spec.hunt_id, won=claimed)
            if not claimed:
                continue  # lost the settle-window race -> another worker has it
            run_began = time.monotonic()
            try:
                self._worker.run(spec, fire)
                executed += 1
                self._metrics.run_completed(
                    spec.hunt_id, duration_seconds=time.monotonic() - run_began
                )
                self._failures.succeeded(spec.hunt_id, fire, now)
            except Exception as exc:
                # One hunt's failure must not end the tick: the others are still due,
                # and the daemon loop above this would exit on an escaping exception.
                self._metrics.run_failed(
                    spec.hunt_id, duration_seconds=time.monotonic() - run_began
                )
                self._metrics.fire_failed(spec.hunt_id, reason=failure_reason(exc))
                self._failures.failed(
                    spec.hunt_id, fire, now, exc, requested=requested.get(spec.hunt_id)
                )
            finally:
                self._coord.release(spec.hunt_id, fire)
        self._metrics.tick_completed(time.monotonic() - tick_began)
        return executed
