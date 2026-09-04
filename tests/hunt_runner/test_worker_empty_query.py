#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_worker_empty_query.py
#  Purpose:      A hunt with nothing to run fails loudly instead of succeeding
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt whose rules compiled to nothing must not advance its watermark.

Advancing it is what made an unrunnable hunt indistinguishable from a working one:
the lease is claimed, the watermark moves, the fire completes, and no row is ever
written. The stand-ins here raise on ANY attribute, so the test proves the worker
refuses BEFORE it reaches ClickHouse or the coordinator rather than proving which
calls it happened to make.
"""

from __future__ import annotations

import pytest

from dfe_engine.hunt_runner import EmptyHuntQuery, HuntSpec, HuntWorker


class _Untouchable:
    """Anything the worker must not reach when there is nothing to run."""

    def __getattr__(self, name: str):
        raise AssertionError(f"worker reached {name} for a hunt with no query")


def test_a_hunt_with_no_queries_raises_before_touching_clickhouse():
    worker = HuntWorker(_Untouchable(), _Untouchable())
    with pytest.raises(EmptyHuntQuery, match="silent_hunt"):
        worker.run(HuntSpec(hunt_id="silent_hunt", interval_seconds=60), scheduled_start=500)


def test_a_hunt_whose_only_query_is_blank_raises_too():
    worker = HuntWorker(_Untouchable(), _Untouchable())
    with pytest.raises(EmptyHuntQuery):
        worker.run(
            HuntSpec(hunt_id="blank", interval_seconds=60, queries=["   ", ""]),
            scheduled_start=500,
        )
