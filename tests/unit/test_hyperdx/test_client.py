#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client.py
#  Purpose:      Tests for HyperDXClient (pure logic, no HTTP)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for HyperDXClient against the dfe-hyperdx fork's REAL API shape.

Tests that require a running HyperDX instance are marked with
``pytest.mark.skip``.  Only pure-logic methods (header minting, breaker
timing, the disconnected short-circuit) are tested here.
"""

from __future__ import annotations

import pytest

from dfe_engine.hyperdx.client import HyperDXClient

# ---------------------------------------------------------------------------
# HyperDXClient construction + headers
# ---------------------------------------------------------------------------


class TestClientConstruction:
    def test_client_strips_trailing_slash(self):
        client = HyperDXClient(base_url="http://example.com/", api_key="key")
        assert client._base_url == "http://example.com"

    def test_client_defaults_connected(self):
        client = HyperDXClient(base_url="http://example.com", api_key="key")
        assert client._connected is True

    def test_headers_include_bearer(self):
        client = HyperDXClient(base_url="http://example.com", api_key="my-key")
        headers = client._headers()
        assert headers["Authorization"] == "Bearer my-key"
        assert headers["Content-Type"] == "application/json"

    def test_token_provider_wins_over_api_key(self):
        client = HyperDXClient(
            base_url="http://x",
            api_key="static-key",
            token_provider=lambda: "minted-jwt",
        )
        assert client._headers()["Authorization"] == "Bearer minted-jwt"

    def test_token_provider_called_per_headers_build(self):
        # a caching provider re-mints near expiry, so every call must consult it
        tokens = iter(["t1", "t2"])
        client = HyperDXClient(base_url="http://x", token_provider=lambda: next(tokens))
        assert client._headers()["Authorization"] == "Bearer t1"
        assert client._headers()["Authorization"] == "Bearer t2"

    def test_api_key_defaults_empty_without_provider(self):
        client = HyperDXClient(base_url="http://x")
        assert client._headers()["Authorization"] == "Bearer "


# ---------------------------------------------------------------------------
# Disconnected short-circuit (no HTTP attempted once _connected=False)
# ---------------------------------------------------------------------------


class TestDisconnectedShortCircuit:
    """When _connected=False, all async methods return immediately."""

    @pytest.mark.asyncio
    async def test_get_team_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.get_team() is None

    @pytest.mark.asyncio
    async def test_get_team_api_key_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.get_team_api_key() is None

    @pytest.mark.asyncio
    async def test_invite_member_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.invite_member("user@corp.com") is False

    @pytest.mark.asyncio
    async def test_source_methods_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.list_sources() is None
        assert await client.create_source({"name": "s"}) is None
        assert await client.update_source("s1", {"name": "s"}) is False
        assert await client.delete_source("s1") is False

    @pytest.mark.asyncio
    async def test_dfe_source_methods_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.put_dfe_source("filebeat", {"kind": "log"}) is None
        assert await client.delete_dfe_source("filebeat") is None
        assert await client.list_dfe_sources() is None


# ---------------------------------------------------------------------------
# Breaker timing: fast failure, finite re-probe window
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.content = b"{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncHttpClient:
    """Stands in for scalo.http.AsyncHttpClient; records constructor kwargs."""

    last_kwargs: dict | None = None
    payload: dict = {"_id": "team-1"}
    fail = False

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, **kwargs):
        if type(self).fail:
            raise ConnectionError("boom")
        return _FakeResponse(type(self).payload)


class TestBreakerTiming:
    @pytest.fixture(autouse=True)
    def _patch_http(self, monkeypatch):
        import scalo.http

        _FakeAsyncHttpClient.fail = False
        _FakeAsyncHttpClient.last_kwargs = None
        monkeypatch.setattr(scalo.http, "AsyncHttpClient", _FakeAsyncHttpClient)

    @pytest.mark.asyncio
    async def test_request_uses_short_timeout_and_retries(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        assert await client.get_team() == {"_id": "team-1"}
        kwargs = _FakeAsyncHttpClient.last_kwargs
        assert kwargs["timeout"] == 5.0
        assert kwargs["retries"] == 1

    @pytest.mark.asyncio
    async def test_failure_sets_finite_reprobe_deadline(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        _FakeAsyncHttpClient.fail = True
        assert await client.get_team() is None
        assert client._connected is False
        assert client._retry_at != float("inf")

    @pytest.mark.asyncio
    async def test_skips_within_window_reprobes_after(self):
        import time as _time

        client = HyperDXClient(base_url="http://x", api_key="k")
        _FakeAsyncHttpClient.fail = True
        await client.get_team()

        # Within the window: short-circuits without touching HTTP.
        _FakeAsyncHttpClient.last_kwargs = None
        assert await client.get_team() is None
        assert _FakeAsyncHttpClient.last_kwargs is None

        # Window elapsed: re-probes and recovers.
        _FakeAsyncHttpClient.fail = False
        client._retry_at = _time.monotonic() - 1
        assert await client.get_team() == {"_id": "team-1"}
        assert client._connected is True

    @pytest.mark.asyncio
    async def test_manual_latch_never_reprobes(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        _FakeAsyncHttpClient.last_kwargs = None
        assert await client.get_team() is None
        assert _FakeAsyncHttpClient.last_kwargs is None


# ---------------------------------------------------------------------------
# HTTP-dependent tests (skipped — requires running HyperDX)
# ---------------------------------------------------------------------------


class TestGetTeam:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_get_team_jit_creates_default_team(self):
        pass


class TestSourcesHttp:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_create_source_success(self):
        pass


class TestInviteMemberHttp:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_invite_member_success(self):
        pass
