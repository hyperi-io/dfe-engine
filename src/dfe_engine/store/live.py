#  Project:      dfe-engine
#  File:         store/live.py
#  Purpose:      A value built from a collection several engine replicas write, kept in step
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A value built from a document-store collection that several engine replicas write.

A replica holding something built from a collection, such as the role definitions or
the OIDC relying party, compares the collection's write counter in ``store_revisions``
with the one it built at, and builds again when another replica has written since.
"""

import asyncio
import threading
import time
from collections.abc import Callable

from scalo.logger import logger

from dfe_engine.http_errors import describe_http_error

REFRESH_SECONDS = 2.0
"""How stale another replica's write may be here: the least time between two checks."""


def _on_event_loop() -> bool:
    """Whether the calling thread is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class Live[T]:
    """A value built from a collection, built again once the collection's write counter moves.

    :meth:`get` compares the counter with the one the value was built at, at most once
    every ``refresh_seconds``, and builds again when it has moved. A write through
    another replica therefore shows here within that floor, and one through this
    replica at once when the writer calls :meth:`refresh`. Building is safe from
    several threads at once: the counter and the value are kept as one pair, so a build
    that finishes after a newer one leaves its older counter beside its value, and the
    next check builds again.

    A :meth:`get` on an event loop's thread never touches the store: it starts the
    check on a background thread and returns the value as built, so the next read
    after the check finishes sees the change.
    """

    def __init__(
        self,
        build: Callable[[], T],
        revision: Callable[[], int],
        *,
        refresh_seconds: float = REFRESH_SECONDS,
    ) -> None:
        """Build the value once.

        Args:
            build: Builds the value from the collection.
            revision: The collection's write counter.
            refresh_seconds: The least time between two checks of the counter.
        """
        self._build = build
        self._revision = revision
        self._refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._checking = False
        self._failing = False
        self._built: tuple[int, T] = self._fresh()
        self._checked = time.monotonic()

    @property
    def value(self) -> T:
        """The value as last built, without looking at the collection."""
        return self._built[1]

    def get(self) -> T:
        """The value, built again first when the collection has moved since it was built."""
        now = time.monotonic()
        if now - self._checked < self._refresh_seconds:
            return self._built[1]
        self._checked = now
        if _on_event_loop():
            self._check_in_background()
            return self._built[1]
        if self._revision() != self._built[0]:
            return self.refresh()
        return self._built[1]

    def refresh(self) -> T:
        """Build the value now, whatever the floor."""
        self._built = self._fresh()
        self._checked = time.monotonic()
        return self._built[1]

    def _fresh(self) -> tuple[int, T]:
        # The counter first: a write landing during the build leaves it behind, so the next check builds again.
        revision = self._revision()
        return revision, self._build()

    def _check_in_background(self) -> None:
        """Start one background check, unless one is already running."""
        with self._lock:
            if self._checking:
                return
            self._checking = True
        threading.Thread(target=self._check, name="live-check", daemon=True).start()

    def _check(self) -> None:
        """Build again when the counter has moved, keeping the value as built when the store fails."""
        try:
            if self._revision() != self._built[0]:
                self.refresh()
        except Exception as exc:
            # Logged when it starts and when it stops, so a store that stays down logs once.
            if not self._failing:
                logger.warning(
                    "A replicated value could not be checked against its store; serving it as built",
                    **describe_http_error(exc),
                )
            self._failing = True
        else:
            if self._failing:
                logger.info("A replicated value is being checked against its store again")
            self._failing = False
        finally:
            with self._lock:
                self._checking = False
