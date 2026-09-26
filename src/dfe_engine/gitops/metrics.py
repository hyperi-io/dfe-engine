#  Project:      dfe-engine
#  File:         gitops/metrics.py
#  Purpose:      What the deploy-repo clone reports about its batched writes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deploy-repo clone's instruments, as scalo metrics.

A batch lands many writes as one commit, so the writes it throws away are invisible
in the deploy repo's history: an error inside the block, or a push the remote
refused, leaves nothing behind but this counter. The engine registers it on the
metrics manager its service framework serves on ``/metrics``, so it carries that
manager's namespace. With no manager every record call returns without doing
anything, which is the state the unit suite and the CLI run in.
"""

from typing import Any, Literal

BATCHES = "gitops_batches_total"

BatchOutcome = Literal["committed", "unchanged", "split", "discarded", "failed"]
"""How a batch ended.

- ``committed``: landed one commit.
- ``unchanged``: had nothing to commit.
- ``split``: landed early, because a review-branch write needed a commit to branch from.
- ``discarded``: dropped its staged writes on an error inside the block.
- ``failed``: committed but could not push.
"""


class GitopsMetrics:
    """The clone's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._batches = manager.counter(
            BATCHES, "Batched deploy-repo writes, by how the batch ended", ["outcome"]
        )

    @property
    def enabled(self) -> bool:
        """Whether a backend is wired, so a caller can skip work nothing reads."""
        return self._manager is not None

    def batch(self, outcome: BatchOutcome) -> None:
        """Record how one batch ended."""
        if self._manager is None:
            return
        self._batches.labels(outcome=outcome).inc()


__all__ = ["BATCHES", "BatchOutcome", "GitopsMetrics"]
