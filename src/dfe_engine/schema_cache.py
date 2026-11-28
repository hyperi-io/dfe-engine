#  Project:      dfe-engine
#  File:         schema_cache.py
#  Purpose:      Register the caches over the configured schema tree, so a re-seed clears them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The caches that hold a read of the configured schema tree.

A re-seed rewrites that tree in place, so a read made before it is the old
version's. Each such cache registers here, and the re-seed clears them all.
"""

from functools import _CacheInfo
from typing import Protocol


class _Cached(Protocol):
    def cache_clear(self) -> None: ...

    def cache_info(self) -> _CacheInfo: ...  # noqa: V105


_registered: list[_Cached] = []


def reseed_clears[C: _Cached](cached: C) -> C:
    """Register a functools cache that reads the schema tree. Goes above the cache."""
    _registered.append(cached)
    return cached


def registered() -> tuple[_Cached, ...]:
    """Every registered cache, in import order."""
    return tuple(_registered)


def clear_schema_caches() -> None:
    """Drop every registered read of the schema tree."""
    for cached in _registered:
        cached.cache_clear()
