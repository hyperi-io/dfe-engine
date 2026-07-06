#  Project:      dfe-engine
#  File:         resilience.py
#  Purpose:      dfe-local resilience primitives - reconnect-and-retry + trip-and-reject
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-local resilience primitives - ONE home for two complementary shapes.

Two ways to survive a flaky dependency, both live here so every dfe consumer
reaches for the same vocabulary:

- **reconnect-and-retry** (:class:`ReconnectingResilience`) - for a POOLED
  connection you can rebuild: a transient outage backs off, reconnects, and
  recovers automatically on the next success. ClickHouse uses it (its pooled
  client). Generic: the caller injects the error classifiers + a name, so it is
  not coupled to any one backend.
- **trip-and-reject** (:class:`CircuitBreaker`, re-exported from scalo) - for a
  one-shot outbound call you should STOP hammering when it is clearly down:
  after N failures the circuit opens and fast-rejects, then half-opens to probe
  and closes on recovery. HyperDX uses it (best-effort provisioning calls).

``ReconnectingResilience`` is dfe-local for now; its IDEAL home is
``scalo.resilience`` (a sibling to ``CircuitBreaker``) - filed as a scalo-gap in
the handover, not changed here (scalo is release-gated). The CircuitBreaker
re-export means a consumer imports BOTH shapes from this one dfe module.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import TypeVar

from scalo.logger import logger

# trip-and-reject: re-exported so dfe consumers get both resilience shapes here.
from scalo.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
    CircuitState,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitState",
    "OutageState",
    "ReconnectingResilience",
    "ResilienceConfig",
    "ServiceUnavailable",
]

T = TypeVar("T")


class OutageState(StrEnum):
    """A reconnecting layer's view of a dependency's availability."""

    HEALTHY = "healthy"
    TRANSIENT = "transient"
    WAKING = "waking"
    DEAD = "dead"


class ServiceUnavailable(Exception):
    """A dependency was unreachable after the resilience budget was exhausted.

    A surface maps this to 503 (retryable), never a 500/crash. ``waking`` is True
    when the outage was a known warm-up so the surface can say "warming up".
    """

    def __init__(self, message: str, *, waking: bool = False) -> None:
        super().__init__(message)
        self.waking = waking


@dataclass(slots=True)
class ResilienceConfig:
    """Back-off + budget knobs (config-cascade; NEVER hardcoded at the call site).

    Sourced from the consumer's settings so an operator can widen the budget for a
    slow cold start without a code change.
    """

    enabled: bool = True
    wait_initial: float = 0.5  # first back-off (seconds)
    wait_max: float = 10.0  # back-off cap per attempt (poll cadence once ramped)
    wait_multiplier: float = 2.0  # exponential factor
    budget_seconds: float = 60.0  # GENEROUS backstop for a transient outage
    waking_budget_seconds: float = 300.0  # extended budget once WAKING (cold start)


class ReconnectingResilience:
    """Run an operation with reconnecting back-off + outage-state tracking.

    Generic over the dependency: the caller injects the classifiers and a name so
    the same engine serves any pooled backend (ClickHouse today; another pooled
    connection tomorrow).

    Args:
        config: the back-off + budget knobs (config-cascade).
        name: the dependency's name, for log lines ("ClickHouse", ...).
        is_transient: True when an error is worth retrying at all (a connection
            outage OR a rate-limit); False for a genuine query/logic error, which
            surfaces immediately un-retried.
        is_reconnectable: True when an error is a CONNECTION outage (rebuild the
            pool between attempts); False for a transient-but-connected error (e.g.
            a rate-limit) where a reconnect would be wrong.
        reconnect: tears down the pooled client so the next op rebuilds a fresh one.
        on_connect_failure: OPTIONAL warm-up hook. Called once when a CONNECTION
            outage begins; returns True if it initiated a wake (extends the budget
            to the cold-start window and flips the state to WAKING). Default None.
        unavailable_exc: the exception TYPE raised when the budget is exhausted
            (a :class:`ServiceUnavailable` subclass, so a backend keeps its own
            typed exception that its surface already catches).
        sleep / now: injectable clock (tests pass fakes so they never really sleep
            and never race a wall clock).
    """

    def __init__(
        self,
        config: ResilienceConfig,
        *,
        name: str,
        is_transient: Callable[[Exception], bool],
        is_reconnectable: Callable[[Exception], bool],
        reconnect: Callable[[], None],
        on_connect_failure: Callable[[], bool] | None = None,
        unavailable_exc: type[ServiceUnavailable] = ServiceUnavailable,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._name = name
        self._is_transient = is_transient
        self._is_reconnectable = is_reconnectable
        self._reconnect = reconnect
        self._on_connect_failure = on_connect_failure
        self._unavailable_exc = unavailable_exc
        self._sleep = sleep
        self._now = now
        self._lock = Lock()
        self._state = OutageState.HEALTHY

    @property
    def state(self) -> OutageState:
        """The current outage state (read by the health/readiness surface)."""
        with self._lock:
            return self._state

    @property
    def healthy(self) -> bool:
        """True when the last operation succeeded (readiness = this)."""
        return self.state == OutageState.HEALTHY

    def _set_state(self, state: OutageState) -> None:
        with self._lock:
            self._state = state

    def run(self, op: Callable[[], T]) -> T:
        """Execute *op*, retrying a TRANSIENT error with exponential back-off.

        Returns op()'s result on the first success (state -> HEALTHY). A
        non-transient error (syntax, memory, auth, exec timeout) re-raises
        immediately. A CONNECTION outage reconnects + backs off; a transient-but-
        connected error (e.g. a rate-limit) backs off WITHOUT reconnecting; either
        retries until the budget is exhausted, then raises the configured
        unavailable exception (state -> DEAD).
        """
        if not self._config.enabled:
            return op()
        try:
            result = op()
        except Exception as exc:
            if not self._is_transient(exc):
                raise
            return self._recover(op, exc)
        else:
            self._mark_healthy()
            return result

    def _recover(self, op: Callable[[], T], first_exc: Exception) -> T:
        """Back-off loop after the first transient failure.

        A CONNECTION outage rebuilds the pooled client before each retry (and may
        fire the warm-up hook); a transient-but-connected error just backs off -
        the connection is fine, so a reconnect/wake would be wrong.
        """
        is_conn = self._is_reconnectable(first_exc)
        waking = self._begin_outage(is_conn)
        budget = self._config.waking_budget_seconds if waking else self._config.budget_seconds
        deadline = self._now() + budget
        wait = self._config.wait_initial
        last_exc: Exception = first_exc
        attempt = 0

        while self._now() < deadline:
            remaining = deadline - self._now()
            self._sleep(min(wait, max(0.0, remaining)))
            attempt += 1
            try:
                if is_conn:
                    self._reconnect()  # rebuild the pooled client (connection outage only)
                result = op()
            except Exception as exc:
                if not self._is_transient(exc):
                    # A real (non-transient) error while probing: surface it.
                    raise
                last_exc = exc
                wait = min(wait * self._config.wait_multiplier, self._config.wait_max)
                continue
            else:
                logger.info(f"{self._name} recovered", attempts=attempt, was_waking=waking)
                self._mark_healthy()
                return result

        self._set_state(OutageState.DEAD)
        logger.error(
            f"{self._name} unavailable after resilience budget exhausted",
            budget_seconds=budget,
            attempts=attempt,
            was_waking=waking,
        )
        raise self._unavailable_exc(
            f"{self._name} unreachable after {budget:.0f}s ({attempt} attempts): {last_exc}",
            waking=waking,
        ) from last_exc

    def _begin_outage(self, is_conn: bool) -> bool:
        """Enter an outage: fire the warm-up hook (connection outage only) and pick
        the state.

        Returns True when a wake was initiated (state WAKING, extended budget);
        False for a plain TRANSIENT outage (incl. every rate-limit back-off).
        """
        waking = False
        if is_conn and self._on_connect_failure is not None:
            try:
                waking = bool(self._on_connect_failure())
            except Exception as exc:
                logger.warning(f"{self._name} warm-up hook failed", error=str(exc))
                waking = False
        if waking:
            self._set_state(OutageState.WAKING)
            logger.info(f"{self._name} warming up (auto-wake fired) - waiting, not dead")
        else:
            self._set_state(OutageState.TRANSIENT)
            logger.warning(f"{self._name} unavailable - backing off to retry")
        return waking

    def _mark_healthy(self) -> None:
        with self._lock:
            if self._state != OutageState.HEALTHY:
                logger.info(f"{self._name} connection healthy")
            self._state = OutageState.HEALTHY
