#  Project:      dfe-engine
#  File:         tests/unit/test_datagen/test_stream.py
#  Purpose:      Cadence bounds, sink behaviour, degrade-not-die delivery
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.datagen.models import DatagenError
from dfe_engine.datagen.schema_source import SchemaEventFactory
from dfe_engine.datagen.stream import CollectSink, HttpPostSink, stream_events
from dfe_engine.source.models import SchemaColumn


def make_factory(seed: int = 1) -> SchemaEventFactory:
    columns = [
        SchemaColumn(name="hostname", type="string", expr="@source: host"),
        SchemaColumn(name="message", type="text", expr="@source: message"),
    ]
    return SchemaEventFactory(columns, seed=seed)


class TestStreamBounds:
    async def test_count_bound(self):
        sink = CollectSink()
        emitted = await stream_events(make_factory(), sink, rate_eps=200.0, count=30)
        assert emitted == 30
        assert len(sink.events) == 30
        for event in sink.events:
            assert event["tags"]["synthetic"] is True

    async def test_duration_bound(self):
        sink = CollectSink()
        emitted = await stream_events(make_factory(), sink, rate_eps=50.0, duration_s=0.5)
        # Poisson at 50 eps for 0.5s: mean ~25, generous bounds for CI jitter.
        assert 3 <= emitted <= 80
        assert len(sink.events) == emitted

    async def test_rate_capped_still_completes(self):
        sink = CollectSink()
        emitted = await stream_events(make_factory(), sink, rate_eps=10**6, count=20)
        assert emitted == 20

    async def test_missing_bound_rejected(self):
        with pytest.raises(DatagenError, match="bound"):
            await stream_events(make_factory(), CollectSink(), rate_eps=5.0)

    async def test_bad_rate_rejected(self):
        with pytest.raises(DatagenError, match="rate"):
            await stream_events(make_factory(), CollectSink(), rate_eps=0, count=1)


class TestHttpPostSink:
    async def test_unreachable_endpoint_degrades_not_dies(self):
        # Port 9 (discard) is closed on any sane host - a real connection
        # failure with no service to mock.
        sink = HttpPostSink("http://127.0.0.1:9/ingest", batch_max=2, timeout=1.0)
        emitted = await stream_events(make_factory(), sink, rate_eps=200.0, count=3)
        assert emitted == 3
        assert sink.failed == 3
        assert sink.sent == 0

    def test_bad_batch_max_rejected(self):
        with pytest.raises(DatagenError, match="batch_max"):
            HttpPostSink("http://127.0.0.1:9/", batch_max=0)
