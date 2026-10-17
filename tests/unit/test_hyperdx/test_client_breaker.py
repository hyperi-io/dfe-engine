#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client_breaker.py
#  Purpose:      Only an unreachable HyperDX opens the client's breaker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A refused request fails one call. An unreachable fork fails the next sixty seconds.

Every test talks to a real listener on loopback. A refusal is an HTTP server
answering with a status, an unreachable fork is a closed port or a listener that
never answers, and a call the breaker skips is one the server never sees.
"""

import asyncio
import socket

import pytest

from dfe_engine.hyperdx import client as client_module
from dfe_engine.hyperdx.client import HyperDXClient
from tests.support.loopback import CountingListener
from tests.support.refusing_api import refusing_api

SKIPPED = "HyperDX unreachable, skipping call"
TEAM = {"_id": "team-1"}


@pytest.fixture
def reprobe_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero re-probe window, so the call after a failure is the probe."""
    monkeypatch.setattr(client_module, "_RETRY_AFTER_SECONDS", 0.0)


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fork that never answers costs a fifth of a second instead of five."""
    monkeypatch.setattr(client_module, "_TIMEOUT_SECONDS", 0.2)


def _skips(audit_events: list[dict]) -> int:
    return len([e for e in audit_events if e["event"] == SKIPPED])


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
async def test_a_refused_request_leaves_the_breaker_closed(status: int):
    with refusing_api(status, {"error": "refused"}) as server:
        client = HyperDXClient(base_url=server.base_url, api_key="k")

        assert await client.create_source({"name": "hunts"}) is None
        server.status, server.body = 200, TEAM
        team = await client.get_team()

        assert team == TEAM
        assert server.paths == ["/sources", "/team"]


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_a_server_error_opens_the_breaker(status: int, audit_events: list[dict]):
    with refusing_api(status, {"error": "down"}) as server:
        client = HyperDXClient(base_url=server.base_url, api_key="k")

        assert await client.get_team() is None
        seen = len(server.paths)
        server.status, server.body = 200, TEAM

        assert await client.get_team() is None
        assert len(server.paths) == seen
        assert _skips(audit_events) == 1


async def test_a_refused_connection_opens_the_breaker(audit_events: list[dict]):
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        port = spare.getsockname()[1]
    client = HyperDXClient(base_url=f"http://127.0.0.1:{port}", api_key="k")

    assert await client.get_team() is None
    assert await client.get_team() is None

    assert _skips(audit_events) == 1


@pytest.mark.usefixtures("short_timeout")
async def test_a_timeout_opens_the_breaker():
    listener = CountingListener(hold=True)
    try:
        client = HyperDXClient(base_url=f"http://127.0.0.1:{listener.port}", api_key="k")

        assert await client.get_team() is None
        dialled = listener.connections
        assert dialled >= 1

        assert await client.get_team() is None
        assert listener.connections == dialled
    finally:
        listener.close()


async def test_every_call_is_skipped_while_the_breaker_is_open(audit_events: list[dict]):
    with refusing_api(503, {"error": "down"}) as server:
        client = HyperDXClient(base_url=server.base_url, api_key="k")
        assert await client.get_team() is None
        seen = len(server.paths)

        assert await client.get_team_api_key() is None
        assert await client.invite_member("user@corp.com") is False
        assert await client.list_sources() is None
        assert await client.create_source({"name": "s"}) is None
        assert await client.update_source("s1", {"name": "s"}) is False
        assert await client.delete_source("s1") is False
        assert await client.put_dfe_source("filebeat", {"kind": "log"}) is None
        assert await client.delete_dfe_source("filebeat") is None
        assert await client.list_dfe_sources() is None

        assert len(server.paths) == seen
        assert _skips(audit_events) == 9


@pytest.mark.usefixtures("reprobe_at_once")
async def test_the_breaker_reprobes_once_its_window_has_passed():
    with refusing_api(503, {"error": "down"}) as server:
        client = HyperDXClient(base_url=server.base_url, api_key="k")
        assert await client.get_team() is None

        server.status, server.body = 200, TEAM

        assert await client.get_team() == TEAM


@pytest.mark.usefixtures("reprobe_at_once", "short_timeout")
async def test_a_cancelled_probe_does_not_wedge_the_breaker(audit_events: list[dict]):
    listener = CountingListener(hold=True)
    try:
        client = HyperDXClient(base_url=f"http://127.0.0.1:{listener.port}", api_key="k")
        assert await client.get_team() is None

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(client.get_team(), timeout=0.05)

        assert await client.get_team() is None
        assert _skips(audit_events) == 0
    finally:
        listener.close()


def test_an_unreachable_fork_costs_seconds_not_the_http_clients_default():
    assert client_module._TIMEOUT_SECONDS <= 5.0
    assert client_module._RETRIES == 1
