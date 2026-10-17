#  Project:      dfe-engine
#  File:         startup_stop.py
#  Purpose:      Startup waits a stop signal may end at once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Startup waits a stop signal may end at once.

uvicorn acts on a stop signal only once startup returns, and startup can wait
minutes for ClickHouse or the broker. A wait marked here holds nothing a stop has to
finish -- no deploy-repo write, no DDL -- so the daemon's server ends the process
when a signal lands inside one (:class:`~dfe_engine.api.server.DaemonServer`).
"""

import threading
from collections.abc import Generator
from contextlib import contextmanager

# Process-wide because a signal handler is, and set only by the main thread it runs on.
_main_thread_waiting = threading.Event()


@contextmanager
def stoppable_wait() -> Generator[None]:
    """Mark the block as a wait a stop signal may end, when the main thread runs it.

    The schema retry runs the same waits on its own thread while the main thread may
    be mid-write, so only the main thread's wait counts.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    _main_thread_waiting.set()
    try:
        yield
    finally:
        _main_thread_waiting.clear()


def main_thread_waiting() -> bool:
    """Whether the main thread is inside a :func:`stoppable_wait` block."""
    return _main_thread_waiting.is_set()
