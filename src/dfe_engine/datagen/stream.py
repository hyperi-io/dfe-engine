#  Project:      dfe-engine
#  File:         datagen/stream.py
#  Purpose:      Live-tail cadence + delivery sinks for generated events
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Live-tail streaming of generated events.

Datagen is a demo/test stream, never a load generator: pacing is a Poisson
process at a modest configured rate (interarrival = expovariate draw), which
reads as live telemetry in a tail view - bursts and gaps, no metronome.

Sinks are tiny async callables. ``CollectSink`` gathers in memory (tests,
API responses); ``HttpPostSink`` posts JSON to dfe-receiver so a demo stream
exercises the real default data path end to end.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from scalo.logger import logger

from dfe_engine.datagen.models import DatagenError
from dfe_engine.datagen.schema_source import SchemaEventFactory

# A stream is a demo artefact - cap the rate defensively so a fat-fingered
# config cannot turn it into a load test.
MAX_RATE_EPS = 200.0


class EventSink(Protocol):
    """Anything that can accept generated events."""

    async def __call__(self, event: dict[str, Any]) -> None:
        """Deliver one event."""
        ...  # pragma: no cover - protocol signature

    async def flush(self) -> None:
        """Deliver anything buffered."""
        ...  # pragma: no cover - protocol signature


class CollectSink:
    """Collects events in memory - tests and count-bounded API calls."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    async def flush(self) -> None:
        """Nothing buffered - present for the sink protocol."""


class HttpPostSink:
    """Posts events to an HTTP endpoint (dfe-receiver) in small batches.

    Args:
        url: Full ingest URL (e.g. ``https://receiver.example/ingest/syslog``).
        headers: Extra headers (auth token etc).
        batch_max: Events per POST (1 = one event per request).
        ndjson: Send batches as NDJSON lines instead of a JSON array.
        timeout: Per-request timeout seconds.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        batch_max: int = 10,
        ndjson: bool = False,
        timeout: float = 10.0,
    ) -> None:
        if batch_max < 1:
            raise DatagenError("batch_max must be >= 1")
        self.url = url
        self.headers = headers or {}
        self.batch_max = batch_max
        self.ndjson = ndjson
        self.timeout = timeout
        self.sent = 0
        self.failed = 0
        self._buffer: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= self.batch_max:
            await self.flush()

    async def flush(self) -> None:
        """POST the buffered batch; failures are counted, never fatal."""
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        from scalo.http import AsyncHttpClient

        try:
            async with AsyncHttpClient(base_url=self.url, timeout=self.timeout) as client:
                if self.ndjson:
                    payload = "\n".join(json.dumps(e, default=str) for e in batch)
                    headers = {**self.headers, "Content-Type": "application/x-ndjson"}
                    response = await client.post("", content=payload, headers=headers)
                else:
                    body: Any = batch[0] if len(batch) == 1 else batch
                    response = await client.post("", json=body, headers=self.headers)
                response.raise_for_status()
                self.sent += len(batch)
        except Exception as exc:
            # A demo stream must degrade, not die: count it and stream on.
            self.failed += len(batch)
            logger.warning("datagen POST failed", url=self.url, batch=len(batch), error=str(exc))


async def stream_events(
    factory: SchemaEventFactory,
    sink: EventSink,
    *,
    rate_eps: float = 5.0,
    count: int | None = None,
    duration_s: float | None = None,
) -> int:
    """Stream events at a live-tail cadence until a bound is reached.

    Args:
        factory: Event source.
        sink: Delivery target.
        rate_eps: Mean events per second (Poisson pacing), capped at
            ``MAX_RATE_EPS``.
        count: Stop after this many events.
        duration_s: Stop after this many seconds.

    Returns:
        Events emitted.

    Raises:
        DatagenError: If neither ``count`` nor ``duration_s`` bounds the run,
            or the rate is not positive.
    """
    if count is None and duration_s is None:
        raise DatagenError("stream needs a bound: count and/or duration_s")
    if rate_eps <= 0:
        raise DatagenError("rate_eps must be positive")
    rate = min(rate_eps, MAX_RATE_EPS)

    emitted = 0
    started = time.monotonic()
    rng = factory.pool.rng
    while True:
        if count is not None and emitted >= count:
            break
        if duration_s is not None and time.monotonic() - started >= duration_s:
            break
        await sink(factory.event(when=datetime.now(UTC)))
        emitted += 1
        delay = rng.expovariate(rate)
        remaining = None
        if duration_s is not None:
            remaining = duration_s - (time.monotonic() - started)
            if remaining <= 0:
                break
        await asyncio.sleep(min(delay, remaining) if remaining is not None else delay)
    await sink.flush()
    logger.info(
        "datagen stream complete",
        events=emitted,
        seconds=round(time.monotonic() - started, 1),
        rate_eps=rate,
    )
    return emitted
