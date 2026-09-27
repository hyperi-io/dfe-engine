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
WRITE_RETRIES = "gitops_write_retries_total"
WRITE_BREAKER = "gitops_write_breaker_total"

BatchOutcome = Literal["committed", "unchanged", "split", "discarded", "failed"]
"""How a batch ended.

- ``committed``: landed one commit.
- ``unchanged``: had nothing to commit.
- ``split``: landed early, because a review-branch write needed a commit to branch from.
- ``discarded``: dropped its staged writes on an error inside the block.
- ``failed``: committed but could not push.
"""

WriteOp = Literal["fetch", "push"]
"""The remote call a write makes: the fetch before its commit, or the push after."""

RetryOutcome = Literal["retried", "exhausted"]
"""What a transient failure of a write's remote call led to.

- ``retried``: the call was attempted again inside the write budget.
- ``exhausted``: the budget ran out, so the write failed and the API answered 503.
"""

BreakerEvent = Literal["opened", "rejected", "closed"]
"""What the write breaker did.

- ``opened``: calls in a row ran out of budget, or a probe did, so writes fail at once.
- ``rejected``: a write was answered 503 without calling the forge.
- ``closed``: the forge answered a probe, so writes call it again.
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
        self._write_retries = manager.counter(
            WRITE_RETRIES,
            "Transient failures of a deploy-repo write's remote call, by call and outcome",
            ["op", "outcome"],
        )
        self._write_breaker = manager.counter(
            WRITE_BREAKER,
            "Deploy-repo write breaker transitions, and the writes it answered without "
            "calling the forge",
            ["event"],
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

    def write_retry(self, op: WriteOp, outcome: RetryOutcome) -> None:
        """Record a retry of a write's remote call, or the budget running out on it."""
        if self._manager is None:
            return
        self._write_retries.labels(op=op, outcome=outcome).inc()

    def write_breaker(self, event: BreakerEvent) -> None:
        """Record the write breaker opening or closing, or a write it turned away."""
        if self._manager is None:
            return
        self._write_breaker.labels(event=event).inc()


__all__ = [
    "BATCHES",
    "WRITE_BREAKER",
    "WRITE_RETRIES",
    "BatchOutcome",
    "BreakerEvent",
    "GitopsMetrics",
    "RetryOutcome",
    "WriteOp",
]
