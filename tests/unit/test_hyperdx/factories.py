#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/factories.py
#  Purpose:      Factories for hyperdx unit test inputs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Factories for hyperdx unit test inputs."""

import asyncio
from collections.abc import Awaitable, Callable

from dfe_engine.hyperdx.all_sources import MAX_RETRY_SECONDS, AllSourcesRegistration
from dfe_engine.hyperdx.client import RETRY_AFTER_SECONDS
from dfe_engine.hyperdx.sources import DfeSourcesClient
from tests.unit.test_hyperdx.fake_hyperdx import Exploding, FakeHyperDX


def make_all_sources_registration(
    *,
    client: DfeSourcesClient,
    database: str = "dfe",
    first_retry_seconds: float = RETRY_AFTER_SECONDS,
    max_retry_seconds: float = MAX_RETRY_SECONDS,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> AllSourcesRegistration:
    """Build a hyperdx.all_sources.AllSourcesRegistration."""
    return AllSourcesRegistration(
        client=client,
        database=database,
        first_retry_seconds=first_retry_seconds,
        max_retry_seconds=max_retry_seconds,
        sleep=sleep,
    )


def make_exploding_hyperdx() -> Exploding:
    """Build a HyperDX stand-in whose every call raises."""
    return Exploding()


def make_fake_hyperdx(*, reachable: bool = True) -> FakeHyperDX:
    """Build an in-memory HyperDX stand-in."""
    return FakeHyperDX(reachable=reachable)
