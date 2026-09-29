#  Project:      dfe-engine
#  File:         auth/login_throttle.py
#  Purpose:      Backoff after failed sign-ins, per username and per client address
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Failed sign-in backoff, held in this process.

Each username and each client address keeps a count of recent failures. Past its
threshold a key must wait before the next attempt, and every further failure doubles
the wait up to the configured cap. A waiting attempt is refused before the password
is checked, so it costs no bcrypt, and it is not counted, so hammering a locked key
does not lengthen its wait.

A success clears the username's count. The address keeps its count, or one account
the caller owns would clear the address between guesses at another.

Counts live in memory, bounded to a fixed number of keys with the least recently
failed dropped first; a restart or another replica starts from none. The event loop
is the only caller, so there is no lock.
"""

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from dfe_engine.settings import LoginThrottleSettings

# The first wait past the threshold, in seconds; each further failure doubles it.
_BASE_DELAY_SECONDS = 2.0

# Keys held at once; attacker-chosen usernames must not grow memory without bound.
_MAX_KEYS = 10_000


@dataclass(slots=True)
class _Failures:
    count: int
    last: float
    wait_until: float


class LoginThrottle:
    """Failed sign-in counts and waits, per username and per client address.

    Args:
        settings: Thresholds, the longest wait, and whether it throttles at all.
        now: Monotonic clock, injectable so a test need not sleep.
        max_keys: Keys held at once before the least recently failed are dropped.
    """

    def __init__(
        self,
        settings: LoginThrottleSettings,
        *,
        now: Callable[[], float] = time.monotonic,
        max_keys: int = _MAX_KEYS,
    ) -> None:
        self._settings = settings
        self._now = now
        self._max_keys = max_keys
        self._failures: OrderedDict[tuple[str, str], _Failures] = OrderedDict()

    def retry_after(self, username: str, client: str | None) -> int:
        """Seconds before a sign-in for *username* from *client* may be tried; 0 when it may now."""
        if not self._settings.enabled:
            return 0
        now = self._now()
        waits = [
            entry.wait_until - now
            for key in self._keys(username, client)
            if (entry := self._current(key, now)) is not None
        ]
        longest = max(waits, default=0.0)
        return math.ceil(longest) if longest > 0 else 0

    def failed(self, username: str, client: str | None) -> None:
        """Count a failed sign-in against *username* and *client*."""
        if not self._settings.enabled:
            return
        now = self._now()
        for key in self._keys(username, client):
            entry = self._current(key, now) or _Failures(count=0, last=now, wait_until=0.0)
            entry.count += 1
            entry.last = now
            threshold = self._threshold(key)
            if entry.count >= threshold:
                doubling = min(entry.count - threshold, 32)
                delay = min(_BASE_DELAY_SECONDS * 2**doubling, self._settings.max_delay_seconds)
                entry.wait_until = now + delay
            self._failures[key] = entry
            self._failures.move_to_end(key)
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)

    def succeeded(self, username: str) -> None:
        """Forget *username*'s failures after it signs in."""
        self._failures.pop(("user", username.casefold()), None)

    def _keys(self, username: str, client: str | None) -> list[tuple[str, str]]:
        keys = [("user", username.casefold())]
        if client:
            keys.append(("client", client))
        return keys

    def _threshold(self, key: tuple[str, str]) -> int:
        if key[0] == "user":
            return self._settings.username_failures
        return self._settings.client_failures

    def _current(self, key: tuple[str, str], now: float) -> _Failures | None:
        """The key's failures, or None once it has gone the forget span with none."""
        entry = self._failures.get(key)
        if entry is None:
            return None
        quiet = now - entry.last
        if quiet > self._settings.max_delay_seconds and now >= entry.wait_until:
            del self._failures[key]
            return None
        return entry
