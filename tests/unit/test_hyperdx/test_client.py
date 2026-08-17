#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client.py
#  Purpose:      Tests for HyperDXClient (pure logic, no HTTP)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for HyperDXClient against the dfe-hyperdx fork's REAL API shape.

Tests that require a running HyperDX instance are marked with
``pytest.mark.skip``.  Only pure-logic methods (URL folding, JSON
generation, dedupe branching, SyncResult) are tested here.
"""

from __future__ import annotations

import json

import pytest

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.hyperdx.client import (
    HyperDXClient,
    SyncResult,
    _connection_host_url,
)

# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


class TestSyncResult:
    def test_default_sync_result(self):
        result = SyncResult()
        assert result.teams_created == []
        assert result.teams_failed == []
        assert result.connections_created == 0
        assert result.connections_failed == 0

    def test_sync_result_mutable(self):
        result = SyncResult()
        result.teams_created.append("team-1")
        result.connections_created += 1
        assert result.teams_created == ["team-1"]
        assert result.connections_created == 1

    def test_sync_result_instances_are_independent(self):
        r1 = SyncResult()
        r2 = SyncResult()
        r1.teams_created.append("t1")
        assert r2.teams_created == []


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
# _connection_host_url (the fork's Connection.host is ONE URL field)
# ---------------------------------------------------------------------------


class TestConnectionHostUrl:
    def test_bare_host_and_port_fold_into_url(self):
        assert _connection_host_url("clickhouse", 8123) == "http://clickhouse:8123"

    def test_bare_host_without_port(self):
        assert _connection_host_url("clickhouse", None) == "http://clickhouse"

    def test_existing_url_passes_through_untouched(self):
        assert _connection_host_url("https://ch.example.com:8443", 8123) == (
            "https://ch.example.com:8443"
        )


# ---------------------------------------------------------------------------
# generate_default_connections_json (fork setupDefaults contract, #145)
# ---------------------------------------------------------------------------


class TestGenerateDefaultConnectionsJson:
    def test_empty_config_returns_empty_array(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        config = ConnectionConfig(connections={}, role_connections={})
        result = client.generate_default_connections_json(config)
        assert json.loads(result) == []

    def test_single_connection_matches_fork_schema(self, monkeypatch):
        monkeypatch.setenv("CH_PASSWORD", "secret123")
        client = HyperDXClient(base_url="http://x", api_key="k")
        conn = ClickHouseConnection(
            name="default",
            host="ch.example.com",
            port=8123,
            database="dfe",
            user="admin",
            password_env="CH_PASSWORD",
        )
        config = ConnectionConfig(
            connections={"default": conn},
            role_connections={"admin": "default"},
        )
        result = json.loads(client.generate_default_connections_json(config))
        assert len(result) == 1
        # exact fork Connection shape: username (NOT user), single URL host,
        # no port/database keys - the #145 silent-credential-drop contract
        assert result[0] == {
            "name": "default",
            "host": "http://ch.example.com:8123",
            "username": "admin",
            "password": "secret123",
        }

    def test_multiple_connections(self, monkeypatch):
        monkeypatch.setenv("PW1", "pass1")
        monkeypatch.setenv("PW2", "pass2")
        client = HyperDXClient(base_url="http://x", api_key="k")
        config = ConnectionConfig(
            connections={
                "default": ClickHouseConnection(name="default", host="h1", password_env="PW1"),
                "tenant": ClickHouseConnection(name="tenant", host="h2", password_env="PW2"),
            },
            role_connections={},
        )
        result = json.loads(client.generate_default_connections_json(config))
        assert len(result) == 2
        hosts = {c["host"] for c in result}
        assert hosts == {"http://h1:8123", "http://h2:8123"}

    def test_missing_env_var_uses_empty_string(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        conn = ClickHouseConnection(
            name="default",
            password_env="NONEXISTENT_VAR_XYZ",
        )
        config = ConnectionConfig(connections={"default": conn}, role_connections={})
        result = json.loads(client.generate_default_connections_json(config))
        assert result[0]["password"] == ""

    def test_returns_valid_json_string(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        config = ConnectionConfig(connections={}, role_connections={})
        result = client.generate_default_connections_json(config)
        assert isinstance(result, str)
        json.loads(result)  # Should not raise


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
    async def test_list_connections_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.list_connections() is None

    @pytest.mark.asyncio
    async def test_create_connection_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.create_connection(
            name="conn", host="h", username="u", password="p", port=8123
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_ensure_connection_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.ensure_connection(name="conn", host="h", username="u")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_connection_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        ok = await client.update_connection("c1", {"name": "n", "host": "h", "username": "u"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_delete_connection_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.delete_connection("c1") is False

    @pytest.mark.asyncio
    async def test_source_methods_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        assert await client.list_sources() is None
        assert await client.create_source({"name": "s"}) is None
        assert await client.update_source("s1", {"name": "s"}) is False
        assert await client.delete_source("s1") is False

    @pytest.mark.asyncio
    async def test_sync_no_tenant_reader_returns_empty(self):
        """sync_connections returns empty result if no tenant_reader connection."""
        client = HyperDXClient(base_url="http://x", api_key="k")
        config = ConnectionConfig(connections={}, role_connections={})
        result = await client.sync_connections(config)
        assert result.teams_created == []
        assert result.connections_created == 0

    @pytest.mark.asyncio
    async def test_sync_disconnected_records_team_failure(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        config = ConnectionConfig(
            connections={
                "tenant_reader": ClickHouseConnection(name="tenant_reader", host="h"),
            },
            role_connections={},
        )
        result = await client.sync_connections(config)
        assert result.teams_failed == ["default"]
        assert result.connections_created == 0


# ---------------------------------------------------------------------------
# ensure_connection dedupe branch (collaborators overridden, no HTTP)
# ---------------------------------------------------------------------------


class _CannedClient(HyperDXClient):
    """Overrides the two HTTP collaborators so the dedupe branch runs offline."""

    def __init__(self, listing):
        super().__init__(base_url="http://x", api_key="k")
        self._listing = listing
        self.created: list[str] = []

    async def list_connections(self):
        return self._listing

    async def create_connection(self, *, name, host, username, password="", port=None):
        self.created.append(name)
        return "new-conn-id"


class TestEnsureConnectionDedupe:
    @pytest.mark.asyncio
    async def test_existing_name_returns_its_id_without_create(self):
        client = _CannedClient([{"id": "abc123", "name": "tenant_reader"}])
        conn_id = await client.ensure_connection(name="tenant_reader", host="h", username="u")
        assert conn_id == "abc123"
        assert client.created == []

    @pytest.mark.asyncio
    async def test_missing_name_creates(self):
        client = _CannedClient([{"id": "abc123", "name": "other"}])
        conn_id = await client.ensure_connection(name="tenant_reader", host="h", username="u")
        assert conn_id == "new-conn-id"
        assert client.created == ["tenant_reader"]

    @pytest.mark.asyncio
    async def test_mongo_style_underscore_id_accepted(self):
        client = _CannedClient([{"_id": "abc123", "name": "tenant_reader"}])
        conn_id = await client.ensure_connection(name="tenant_reader", host="h", username="u")
        assert conn_id == "abc123"


# ---------------------------------------------------------------------------
# HTTP-dependent tests (skipped — requires running HyperDX)
# ---------------------------------------------------------------------------


class TestGetTeam:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_get_team_jit_creates_default_team(self):
        pass


class TestConnectionsHttp:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_create_connection_success(self):
        pass

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_update_connection_success(self):
        pass

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_delete_connection_success(self):
        pass


class TestSourcesHttp:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_create_source_success(self):
        pass


class TestInviteMemberHttp:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_invite_member_success(self):
        pass


class TestSyncConnectionsHttp:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_sync_ensures_default_team_and_tenant_reader(self):
        pass
