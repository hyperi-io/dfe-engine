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
on the metrics manager the engine serves on ``/metrics``. So is a request refused
because its account must change its password first. With no manager every record
call returns without doing anything, which is the state the unit suite runs in.
"""

from typing import Any

WRITES_HELD = "api_writes_held_total"
PASSWORD_CHANGE_REFUSALS = "api_password_change_refusals_total"


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
        self._password_change = manager.counter(
            PASSWORD_CHANGE_REFUSALS,
            "Requests refused because the account must change its password first",
            ["method", "area"],
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

    def password_change_refused(self, method: str, area: str) -> None:
        """Record a request refused until its account changes its password.

        Args:
            method: The HTTP method.
            area: The API area the route belongs to -- the first literal segment
                of the matched route, so the label is bounded by the route table.
        """
        if self._manager is None:
            return
        self._password_change.labels(method=method, area=area).inc()


__all__ = ["PASSWORD_CHANGE_REFUSALS", "WRITES_HELD", "ApiMetrics"]
