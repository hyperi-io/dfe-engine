#  Project:      dfe-engine
#  File:         api/metrics.py
#  Purpose:      What the API reports about mutating requests held for their turn
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The API's instruments, as scalo metrics.

A mutating request on the deploy-repo routers runs off the event loop but waits
its turn behind any other one in flight, so a write that queued is counted here,
on the metrics manager the engine serves on ``/metrics``. With no manager every
record call returns without doing anything, which is the state the unit suite
runs in.
"""

from typing import Any

WRITES_HELD = "api_writes_held_total"


class ApiMetrics:
    """The API's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._held = manager.counter(
            WRITES_HELD, "Mutating requests that waited for another to finish", ["method"]
        )

    @property
    def enabled(self) -> bool:
        """Whether a backend is wired, so a caller can skip work nothing reads."""
        return self._manager is not None

    def write_held(self, method: str) -> None:
        """Record a mutating request that found another one in flight."""
        if self._manager is None:
            return
        self._held.labels(method=method).inc()


__all__ = ["WRITES_HELD", "ApiMetrics"]
