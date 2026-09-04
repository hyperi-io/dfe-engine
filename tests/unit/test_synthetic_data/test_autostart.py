#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_autostart.py
#  Purpose:      Standing demo streams - default off, run/retry/cancel behaviour
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import asyncio

import pytest

from dfe_engine.schema.schema_loader import _resolve_package_schemas_root
from dfe_engine.settings import SyntheticDataSettings
from dfe_engine.synthetic_data.autostart import run_standing_stream, start_autostart
from dfe_engine.synthetic_data.models import StreamRequest
from dfe_engine.synthetic_data.service import SyntheticDataService

_SCHEMAS_ROOT = _resolve_package_schemas_root()
SYSLOG = (_SCHEMAS_ROOT / "meta" / "syslog.yaml") if _SCHEMAS_ROOT else None

needs_packs = pytest.mark.skipif(
    SYSLOG is None or not SYSLOG.exists(),
    reason="dfe-schemas package without reference packs",
)


def service(**overrides) -> SyntheticDataService:
    return SyntheticDataService(SyntheticDataSettings(**overrides))


class TestStartAutostart:
    async def test_default_settings_start_nothing(self):
        assert start_autostart(service(), SyntheticDataSettings()) == []

    async def test_schemas_without_receiver_start_nothing(self):
        settings = SyntheticDataSettings(autostart_schemas="meta/syslog")
        assert start_autostart(service(), settings) == []

    @needs_packs
    async def test_bad_pack_skipped_good_pack_started(self):
        settings = SyntheticDataSettings(
            autostart_schemas="meta/nonexistent, meta/syslog",
            autostart_receiver_url="http://127.0.0.1:9/ingest",
        )
        tasks = start_autostart(service(), settings)
        try:
            assert len(tasks) == 1
            assert tasks[0].get_name() == "synthetic-autostart:meta/syslog"
        finally:
            for task in tasks:
                task.cancel()

    @needs_packs
    async def test_cancel_stops_the_stream(self):
        settings = SyntheticDataSettings(
            autostart_schemas="meta/syslog",
            autostart_receiver_url="http://127.0.0.1:9/ingest",
        )
        tasks = start_autostart(service(), settings)
        await asyncio.sleep(0.2)
        for task in tasks:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]


class TestRunStandingStream:
    async def test_segments_restart_until_cancelled(self):
        # The claim is the loop: a finished segment starts another until the task
        # is cancelled. A stub segment keeps real sockets and a real event pack
        # out of it; the socket version crashed xdist workers under load. The stub
        # signals the third segment rather than the test sleeping for it, so a
        # loaded xdist worker cannot lose the race.
        segments = 0
        third = asyncio.Event()

        async def fake_run_stream(request, *, task=None):
            nonlocal segments
            segments += 1
            if segments >= 3:
                third.set()
            await asyncio.sleep(0)  # the real segment awaits; without this the loop starves
            return {"emitted": request.count, "sent": 0, "failed": 0}

        svc = service()
        svc.run_stream = fake_run_stream  # type: ignore[method-assign]
        request = StreamRequest(
            schema_ref="meta/syslog",
            receiver_url="http://127.0.0.1:9/ingest",
            rate_eps=200.0,
            count=2,
        )
        task = asyncio.get_running_loop().create_task(
            run_standing_stream(svc, request, retry_seconds=0.05)
        )
        await asyncio.wait_for(third.wait(), timeout=10)
        assert not task.done()
        assert segments >= 3, "the loop must have restarted the segment"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_failed_segment_is_retried_not_fatal(self):
        calls = 0

        async def failing_run_stream(request, *, task=None):
            nonlocal calls
            calls += 1
            raise RuntimeError("receiver refused")

        svc = service()
        svc.run_stream = failing_run_stream  # type: ignore[method-assign]
        request = StreamRequest(schema_ref="meta/syslog", receiver_url="http://127.0.0.1:9/ingest")
        task = asyncio.get_running_loop().create_task(
            run_standing_stream(svc, request, retry_seconds=0.01)
        )
        await asyncio.sleep(0.1)
        assert not task.done()
        assert calls >= 3, "a failed segment must be retried"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
