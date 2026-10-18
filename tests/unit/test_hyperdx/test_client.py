#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client.py
#  Purpose:      Tests for HyperDXClient (pure logic, no HTTP)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for HyperDXClient against the dfe-hyperdx fork's REAL API shape.

Tests that require a running HyperDX instance are marked with
``pytest.mark.skip``.  Only pure-logic methods (header minting) are tested here.
The breaker is covered against local listeners in ``test_client_breaker.py``.
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
