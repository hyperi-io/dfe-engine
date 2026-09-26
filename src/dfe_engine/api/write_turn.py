#  Project:      dfe-engine
#  File:         api/write_turn.py
#  Purpose:      Mutating requests take turns without holding the event loop
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One mutating request at a time on the routers whose handlers run on worker threads.

A write there reads, changes and commits several deploy-repo documents, and on a
Compose deployment renders the app configs through fixed temporary paths, so two
running side by side would interleave those steps. The turn is awaited on the event
loop, so a queued write holds no worker thread and no read waits for it.
"""

from collections.abc import AsyncIterator

import anyio
from fastapi import Depends, Request

from dfe_engine.api.metrics import ApiMetrics

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def take_write_turn(request: Request) -> AsyncIterator[None]:
    """Hold a mutating request until no other one is in flight; reads pass straight through."""
    if request.method in _READ_METHODS:
        yield
        return
    state = request.app.state
    lock = getattr(state, "write_turn", None)
    if lock is None:
        # Created on the event loop with no await between the check and the set.
        lock = state.write_turn = anyio.Lock()
    if lock.locked():
        metrics = getattr(state, "api_metrics", None) or ApiMetrics()
        metrics.write_held(request.method)
    async with lock:
        yield


WRITE_TURN = Depends(take_write_turn)
"""The dependency a router or route declares to put its writes in turn."""

__all__ = ["WRITE_TURN", "take_write_turn"]
