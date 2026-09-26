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
refused, leaves nothing behind but this counter. The instruments are created
through scalo's manager, which applies the ``dfe`` namespace from the deployment
contract, so they land as ``dfe_gitops_*``. With no backend wired every record call
returns without doing anything, which is the state the unit suite and the CLI run in.
"""

import functools
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


@functools.cache
def create(app_name: str = "dfe-engine") -> GitopsMetrics:
    """Build the instrument set on scalo's metrics backend, once per process.

    Every manager scalo builds starts its own exporter, so a second call hands back
    the first set. The namespace comes from the deployment contract rather than a
    literal, so these carry the same ``dfe`` prefix as the rest of the product.
    """
    from scalo.metrics import create_metrics

    from dfe_engine.deployment_contract import engine_deployment_contract

    return GitopsMetrics(
        create_metrics(app_name, metric_prefix=engine_deployment_contract().metric_prefix)
    )


__all__ = ["BATCHES", "BatchOutcome", "GitopsMetrics", "create"]
