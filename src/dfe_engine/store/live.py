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

import time
from collections.abc import Callable

REFRESH_SECONDS = 2.0
"""How stale another replica's write may be here: the least time between two checks."""


class Live[T]:
    """A value built from a collection, built again once the collection's write counter moves.

    :meth:`get` compares the counter with the one the value was built at, at most once
    every ``refresh_seconds``, and builds again when it has moved. A write through
    another replica therefore shows here within that floor, and one through this
    replica at once when the writer calls :meth:`refresh`. Building is safe from
    several threads at once: the counter and the value are kept as one pair, so a build
    that finishes after a newer one leaves its older counter beside its value, and the
    next check builds again.
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
        self._built: tuple[int, T] = self._fresh()
        self._checked = time.monotonic()

    @property
    def value(self) -> T:
        """The value as last built, without looking at the collection."""
        return self._built[1]

    def get(self) -> T:
        """The value, built again first when the collection has moved since it was built."""
        now = time.monotonic()
        if now - self._checked >= self._refresh_seconds:
            self._checked = now
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
