#  Project:      dfe-engine
#  File:         tests/unit/test_synthetic_data/test_autostart.py
#  Purpose:      Standing demo streams - default off, run/retry/cancel behaviour
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import asyncio

import pytest

from dfe_engine.schema.schema_loader import _resolve_package_schemas_root
from dfe_engine.settings import SyntheticDataSettings
from dfe_engine.synthetic_data.autostart import run_standing_stream, start_autostart
from dfe_engine.synthetic_data.credential import StreamCredential
from dfe_engine.synthetic_data.models import StreamRequest
from dfe_engine.synthetic_data.service import SyntheticDataService
from tests.unit.test_synthetic_data.factories import make_stream_credential

_SCHEMAS_ROOT = _resolve_package_schemas_root()
SYSLOG = (_SCHEMAS_ROOT / "meta" / "syslog.yaml") if _SCHEMAS_ROOT else None

needs_packs = pytest.mark.skipif(
    SYSLOG is None or not SYSLOG.exists(),
    reason="dfe-schemas package without reference packs",
)


def service(**overrides) -> SyntheticDataService:
    return SyntheticDataService(SyntheticDataSettings(**overrides))


_OWN = "https://receiver.dfe.example/ingest"


@pytest.mark.parametrize(
    ("settings", "url", "presents"),
    [
        ({"autostart_receiver_url": _OWN}, _OWN, True),
        ({"autostart_receiver_url": _OWN}, "https://RECEIVER.dfe.example:8443/other", True),
        ({"allowed_receiver_hosts": " Receiver.Dfe.Example "}, _OWN, True),
        ({"autostart_receiver_url": _OWN}, "http://receiver.dfe.example/ingest", False),
        ({"autostart_receiver_url": "http://receiver.dfe.example/"}, _OWN, True),
        ({"autostart_receiver_url": _OWN}, "https://attacker.example/ingest", False),
        ({"autostart_receiver_url": _OWN}, "https://receiver.dfe.example@attacker.example/", False),
        ({"autostart_receiver_url": _OWN}, "https://receiver.dfe.example.attacker.example/", False),
        ({}, _OWN, False),
        ({"allowed_receiver_hosts": ","}, _OWN, False),
        ({"autostart_receiver_url": _OWN}, "https:///ingest", False),
    ],
    ids=[
        "own_receiver",
        "own_host_any_port_or_case",
        "listed_host",
        "own_host_over_http",
        "http_autostart_names_the_host",
        "foreign_host",
        "userinfo_names_another_host",
        "suffix_of_another_host",
        "nothing_configured",
        "empty_host_list",
        "no_host",
    ],
)
def test_the_credential_goes_only_to_the_own_receiver_over_https(settings, url, presents):
    assert service(**settings).presents_credential(url) is presents


@pytest.mark.parametrize(
    ("url", "headers", "credential", "auth"),
    [
        (_OWN, None, True, {"Authorization": "Bearer segment-token"}),
        ("http://receiver.dfe.example/ingest", None, True, {}),
        ("https://attacker.example/ingest", None, True, {}),
        (_OWN, {"authorization": "Bearer mine"}, True, {"authorization": "Bearer mine"}),
        (
            "https://attacker.example/",
            {"Authorization": "Bearer mine"},
            True,
            {"Authorization": "Bearer mine"},
        ),
        (_OWN, None, False, {}),
    ],
    ids=[
        "own_https",
        "own_http",
        "foreign",
        "caller_auth_stands",
        "caller_auth_anywhere",
        "no_credential",
    ],
)
def test_a_stream_presents_the_token_only_where_it_may(url, headers, credential, auth):
    held = make_stream_credential(token="segment-token") if credential else None
    request = StreamRequest(schema_ref="meta/syslog", receiver_url=url, count=1, headers=headers)
    sent, refused_until = service(autostart_receiver_url=_OWN).stream_auth(request, held)
    expect_retry = (held is not None) and ("Bearer segment-token" in sent.values())
    assert (sent, refused_until) == (auth, held.refused_until if expect_retry else 0.0)


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
        # out of it. The socket version crashed xdist workers under load. The stub
        # signals the third segment rather than the test sleeping for it, so a
        # loaded xdist worker cannot lose the race.
        segments = 0
        third = asyncio.Event()

        async def fake_run_stream(request, *, credential=None, task=None):
            nonlocal segments
            segments += 1
            if segments >= 3:
                third.set()
            await asyncio.sleep(0)  # the real segment awaits, without this the loop starves
            return {"emitted": request.count, "sent": 0, "failed": 0}

        svc = service()
        svc.run_stream = fake_run_stream  # ty: ignore[invalid-assignment]  # the test replaces this service's stream segment
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

    @pytest.mark.parametrize(
        ("url", "minted"),
        [(_OWN, True), ("http://receiver.dfe.example/ingest", False)],
        ids=["own_https_receiver", "http_receiver_mints_nothing"],
    )
    async def test_each_segment_presents_the_credential(self, url, minted):
        def mint() -> StreamCredential:
            mints.append(held)
            return held

        async def fake_run_stream(request, *, credential=None, task=None):
            presented.append(credential)
            raise asyncio.CancelledError

        held = make_stream_credential(token="segment-token")
        mints: list[StreamCredential] = []
        presented: list[StreamCredential | None] = []
        svc = service(autostart_receiver_url=url)
        svc.run_stream = fake_run_stream  # ty: ignore[invalid-assignment]  # the test replaces this service's stream segment
        request = StreamRequest(schema_ref="meta/syslog", receiver_url=url, count=1)
        with pytest.raises(asyncio.CancelledError):
            await run_standing_stream(svc, request, credential=mint, retry_seconds=0.05)
        assert (presented, mints) == (([held], [held]) if minted else ([None], []))

    async def test_failed_segment_is_retried_not_fatal(self):
        calls = 0

        async def failing_run_stream(request, *, credential=None, task=None):
            nonlocal calls
            calls += 1
            raise RuntimeError("receiver refused")

        svc = service()
        svc.run_stream = failing_run_stream  # ty: ignore[invalid-assignment]  # the test replaces this service's stream segment
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
