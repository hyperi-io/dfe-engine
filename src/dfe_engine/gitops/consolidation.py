#  Project:      dfe-engine
#  File:         gitops/consolidation.py
#  Purpose:      Consolidate the deploy-repo clone's packs on a timer, off the request path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Consolidate the deploy-repo clone's packs from a background task.

dulwich's own gc is switched off in the clone, because it ran inside writes, reads
and pushes and rewrote every object the repo holds while the caller waited. The API
lifespan runs :func:`consolidate_forever` instead, beside its other background tasks.
"""

import asyncio

from scalo.logger import logger

from .repo import GitopsRepo


async def consolidate_forever(repo: GitopsRepo, interval_seconds: float) -> None:
    """Ask the clone to consolidate its packs every ``interval_seconds`` until cancelled.

    Each check runs on a worker thread, because it waits for the clone's writer and
    tree locks and the event loop keeps serving meanwhile. A check that raises is
    logged and the next interval tries again, so one bad run never ends the task.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(repo.consolidate_if_due)
        except Exception:
            logger.exception("Gitops pack consolidation raised; the next interval tries again")


__all__ = ["consolidate_forever"]
