#  Project:      dfe-engine
#  File:         tests/support/races.py
#  Purpose:      Start several calls at one instant, as replicas make them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Run calls from their own threads behind one barrier, so they reach the store together."""

import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor


def race[R](calls: Sequence[Callable[[], R]]) -> list[R | BaseException]:
    """Run every call at once from its own thread, and return each result or what it raised."""
    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], R]) -> R | BaseException:
        barrier.wait(timeout=60)
        try:
            return call()
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def raised(results: Sequence[object]) -> list[BaseException]:
    """The exceptions among *results*."""
    return [r for r in results if isinstance(r, BaseException)]
