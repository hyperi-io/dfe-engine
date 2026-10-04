#  Project:      dfe-engine
#  File:         hunt_runner/connect.py
#  Purpose:      Wait out a ClickHouse that is down, or does not know the runner's user yet
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The runner's way through a ClickHouse it cannot use yet, without exiting.

The engine mints ``dfe_hunt_runner`` in its CH RBAC reconcile, and retries that
reconcile in the background when ClickHouse was down at its own boot. Until the
user exists the runner's login is refused, which is the same wait as a refused
connection: the runner backs off and tries again rather than crash-looping its
pod. Anything else -- a bad config, a missing table -- still surfaces.
"""

import math
from collections.abc import Callable

from scalo.logger import logger
from scalo.resilience import ReconnectingResilience, ResilienceConfig

from dfe_engine.backoff import jittered_sleep
from dfe_engine.clickhouse.errors import is_connection_error, is_identity_error
from dfe_engine.settings import ClickHouseResilienceSettings

from .metrics import HuntRunnerMetrics


def unavailable_reason(exc: BaseException) -> str | None:
    """Why ClickHouse could not be used, when waiting can fix it; None otherwise.

    Returns:
        ``connection`` for an outage, ``authentication`` for a refused user.
    """
    if is_connection_error(exc):
        return "connection"
    if is_identity_error(exc):
        return "authentication"
    return None


def connect_when_available[T](
    attempt: Callable[[], T],
    *,
    resilience: ClickHouseResilienceSettings,
    metrics: HuntRunnerMetrics,
    user: str,
    sleep: Callable[[float], None] = jittered_sleep,
) -> T:
    """Run *attempt* until it gets through, backing off while ClickHouse cannot be used.

    The back-off steps are ``clickhouse.resilience``'s, each jittered. There is no
    budget: the runner has nothing to do until it connects, so it waits as long as
    the outage or the missing user lasts. The first refusal is logged with its
    cause; every one is counted.

    Args:
        attempt: builds the client and whatever must hold before ticking; it is
            called afresh each time, so it cleans up after a failure itself.
        resilience: the back-off settings.
        metrics: where each refusal is counted.
        user: the ClickHouse user the runner connects as, for the log line.
        sleep: how a back-off is waited out.

    Returns:
        What *attempt* returned.

    Raises:
        Exception: Whatever *attempt* raised that waiting cannot fix.
    """
    refusals = 0

    def counted() -> T:
        nonlocal refusals
        try:
            return attempt()
        except Exception as exc:
            reason = unavailable_reason(exc)
            if reason is not None:
                refusals += 1
                metrics.clickhouse_unavailable(stage="connect", reason=reason)
                if refusals == 1:
                    logger.warning(
                        "hunt runner cannot use ClickHouse yet; retrying with back-off",
                        user=user,
                        reason=reason,
                        error=str(exc),
                    )
            raise

    config = ResilienceConfig(
        **resilience.model_dump(exclude={"budget_seconds", "waking_budget_seconds"}),
        budget_seconds=math.inf,
        waking_budget_seconds=math.inf,
    )
    retrying = ReconnectingResilience(
        config,
        name="ClickHouse",
        is_transient=lambda exc: unavailable_reason(exc) is not None,
        # Each attempt builds its own client, so there is no pooled one to rebuild.
        is_reconnectable=lambda _exc: False,
        reconnect=lambda: None,
        sleep=sleep,
    )
    return retrying.run(counted)


class TickGuard:
    """Skip a tick ClickHouse refused, rather than letting it end the process.

    The daemon runs the next tick on its poll interval, which is the back-off. The
    outage is logged once when it starts and once when it clears; every skipped
    tick is counted.

    Args:
        tick: the runner's tick.
        metrics: where each skipped tick is counted.
    """

    def __init__(self, tick: Callable[[int], int], *, metrics: HuntRunnerMetrics) -> None:
        self._tick = tick
        self._metrics = metrics
        self._down = False

    def __call__(self, now: int) -> int:
        """Run one tick; returns the runs it executed, 0 for a skipped tick."""
        try:
            executed = self._tick(now)
        except Exception as exc:
            reason = unavailable_reason(exc)
            if reason is None:
                raise
            self._metrics.clickhouse_unavailable(stage="tick", reason=reason)
            if not self._down:
                logger.warning(
                    "hunt runner tick skipped: ClickHouse cannot be used",
                    reason=reason,
                    error=str(exc),
                )
            self._down = True
            return 0
        if self._down:
            logger.info("hunt runner ticks running again")
        self._down = False
        return executed
