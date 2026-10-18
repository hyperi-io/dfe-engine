#  Project:      dfe-engine
#  File:         hyperdx/all_sources.py
#  Purpose:      The HyperDX source over the all_sources Merge table, on every team
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The ``all_sources`` HyperDX source, which the DFE Throughput dashboard reads every source's rows through.

The fork skips a provisioned dashboard on any team missing a source it reads, so a HyperDX that boots after the engine must still end up with this source: :class:`AllSourcesRegistration` keeps trying, backing off while HyperDX stays away.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from scalo.logger import logger

from dfe_engine.clickhouse.all_sources import (
    ALL_SOURCES,
    ALL_SOURCES_COLUMNS,
    ALL_SOURCES_TIMESTAMP,
)
from dfe_engine.http_errors import describe_http_error
from dfe_engine.hyperdx.client import RETRY_AFTER_SECONDS
from dfe_engine.hyperdx.sources import DfeSourcesClient, ensure_source

__all__ = [
    "MAX_RETRY_SECONDS",
    "AllSourcesRegistration",
    "register_all_sources",
]

# The longest wait between registration attempts while HyperDX stays down or keeps refusing.
MAX_RETRY_SECONDS = 900.0


async def register_all_sources(*, client: DfeSourcesClient, database: str) -> list[str] | None:
    """Create or update the all_sources HyperDX source on every team; None when HyperDX did not take it."""
    return await ensure_source(
        client=client,
        columns=ALL_SOURCES_COLUMNS,
        database=database,
        name=ALL_SOURCES,
        table=ALL_SOURCES,
        timestamp=ALL_SOURCES_TIMESTAMP,
    )


class AllSourcesRegistration:
    """Register the all_sources HyperDX source, trying again until HyperDX takes it.

    The wait starts at *first_retry_seconds* and doubles after each failed attempt up to *max_retry_seconds*, so a HyperDX that is down, or refuses the source for good, costs one attempt every few minutes rather than one a minute.
    """

    def __init__(
        self,
        *,
        client: DfeSourcesClient,
        database: str,
        first_retry_seconds: float = RETRY_AFTER_SECONDS,
        max_retry_seconds: float = MAX_RETRY_SECONDS,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        """Hold the client and the back-off; nothing runs until attempt or run."""
        self._client = client
        self._database = database
        self._failing = False
        self._first_retry_seconds = first_retry_seconds
        self._max_retry_seconds = max_retry_seconds
        self._sleep = sleep

    def _failed(self, *, fields: Mapping[str, object]) -> None:
        """Warn on the first failure of a run of them, and stay quiet for the rest."""
        if not (self._failing):
            logger.warning(
                "HyperDX all_sources source not registered; trying again until it is",
                max_retry_seconds=self._max_retry_seconds,
                retry_seconds=self._first_retry_seconds,
                **fields,
            )
        self._failing = True

    async def attempt(self) -> bool:
        """Try the registration once and report whether HyperDX took it."""
        try:
            teams = await register_all_sources(client=self._client, database=self._database)
        except Exception as exc:  # the retry outlives any one failure
            self._failed(fields=describe_http_error(exc=exc))
            return False
        if teams is None:
            self._failed(fields={})
            return False
        logger.info("HyperDX all_sources source registered", teams=len(teams))
        self._failing = False
        return True

    async def run(self) -> None:
        """Attempt the registration until HyperDX takes it, doubling the wait after each failure."""
        wait_seconds = self._first_retry_seconds
        while not (await self.attempt()):
            # Positional: the Callable type fixes no parameter name.
            await self._sleep(wait_seconds)
            wait_seconds = min(wait_seconds * 2, self._max_retry_seconds)
