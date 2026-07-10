#  Project:      dfe-engine
#  File:         clickhouse/resilience.py
#  Purpose:      ClickHouse resilience - the CH binding of ReconnectingResilience
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse connection resilience: survive a CH outage and recover.

This is the ClickHouse BINDING of the generic
:class:`~dfe_engine.resilience.ReconnectingResilience` (the reconnect-and-retry
engine) - it injects the CH error classifiers and a CH-typed unavailable
exception, nothing more. The generic engine lives in ``dfe_engine.resilience``;
its shape and the "waking, not dead" outage state are documented there.

Every CH operation runs through :meth:`ChResilience.run`: a CONNECTION failure
(transport-level, or a CH connection error code) retries with reconnecting
back-off; a RATE_LIMITED (202 TOO_MANY_SIMULTANEOUS_QUERIES) error backs off
WITHOUT reconnecting (the connection is fine, the server is busy); a QUERY-LEVEL
error (syntax, memory, access, exec timeout) surfaces immediately un-retried.
Recovery is signal-gated, not timer-raced (the "no timing flake" rule): the
back-off returns the INSTANT the real operation succeeds, and the budget is a
GENEROUS config-cascade backstop, never a value we race against.
"""

from __future__ import annotations

from ..resilience import (
    OutageState,
    ReconnectingResilience,
    ResilienceConfig,
    ServiceUnavailable,
)

# The error taxonomy is the SSoT for classification + the retry decision. A
# CONNECTION outage reconnects; a RATE_LIMITED (202) error backs off WITHOUT a
# reconnect. Re-exported so ``from ...resilience import is_connection_error``
# still resolves for existing callers.
from .errors import is_connection_error, is_retryable_error

__all__ = [
    "ChResilience",
    "ChUnavailable",
    "OutageState",
    "ResilienceConfig",
    "is_connection_error",
    "is_retryable_error",
]


class ChUnavailable(ServiceUnavailable):
    """CH is unreachable after the resilience budget was exhausted.

    A :class:`~dfe_engine.resilience.ServiceUnavailable` subtype so the API layer's
    CH-specific handling (503, "warming up" when ``waking``) keeps working while
    the raise site lives in the generic engine.
    """


class ChResilience(ReconnectingResilience):
    """The ClickHouse binding of :class:`ReconnectingResilience`.

    Fixes the CH classifiers + name + typed exception; the reconnect and optional
    auto-wake hooks are injected by the manager (see ``clickhouse_manager.py``).
    """

    def __init__(
        self,
        config: ResilienceConfig,
        *,
        reconnect,
        on_connect_failure=None,
        sleep=None,
        now=None,
    ) -> None:
        kwargs = {}
        if sleep is not None:
            kwargs["sleep"] = sleep
        if now is not None:
            kwargs["now"] = now
        super().__init__(
            config,
            name="ClickHouse",
            is_transient=is_retryable_error,
            is_reconnectable=is_connection_error,
            reconnect=reconnect,
            on_connect_failure=on_connect_failure,
            unavailable_exc=ChUnavailable,
            **kwargs,
        )
