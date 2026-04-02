#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client.py
#  Purpose:      Tests for HyperDXClient (pure logic, no HTTP)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for HyperDXClient.

Tests that require a running HyperDX instance are marked with
``pytest.mark.skip``.  Only pure-logic methods (JSON generation,
SyncResult) are tested here.
"""

from __future__ import annotations

import json

import pytest

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.hyperdx.client import HyperDXClient, SyncResult

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
# HyperDXClient construction
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


# ---------------------------------------------------------------------------
# generate_default_connections_json (pure logic, no HTTP)
# ---------------------------------------------------------------------------


class TestGenerateDefaultConnectionsJson:
    def test_empty_config_returns_empty_array(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        config = ConnectionConfig(connections={}, role_connections={})
        result = client.generate_default_connections_json(config)
        assert json.loads(result) == []

    def test_single_connection(self, monkeypatch):
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
        assert result[0]["name"] == "default"
        assert result[0]["host"] == "ch.example.com"
        assert result[0]["port"] == 8123
        assert result[0]["database"] == "dfe"
        assert result[0]["user"] == "admin"
        assert result[0]["password"] == "secret123"

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
        assert hosts == {"h1", "h2"}

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
# HTTP-dependent tests (skipped — requires running HyperDX)
# ---------------------------------------------------------------------------


class TestDisconnectedShortCircuit:
    """When _connected=False, all async methods return immediately."""

    @pytest.mark.asyncio
    async def test_create_team_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.create_team("team-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_create_connection_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.create_connection(
            team_id="t1",
            name="conn",
            host="h",
            port=8123,
            database="db",
            user="u",
            password="p",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_connection_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.delete_connection(team_id="t1", conn_id="c1")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_team_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.delete_team(team_id="t1")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_connection_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.update_connection(
            team_id="t1",
            connection_id="c1",
            host="newhost",
            port=8123,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_sync_no_tenant_reader_returns_empty(self):
        """sync_connections returns empty result if no tenant_reader connection."""
        from dfe_engine.orgs.models import Org

        client = HyperDXClient(base_url="http://x", api_key="k")
        config = ConnectionConfig(connections={}, role_connections={})
        result = await client.sync_connections(
            orgs=[Org(name="acme", org_ids=["acme"])],
            conn_config=config,
        )
        assert result.teams_created == []
        assert result.connections_created == 0


class TestCreateTeam:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_create_team_success(self):
        pass

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_create_team_failure_sets_disconnected(self):
        pass


class TestCreateConnection:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_create_connection_success(self):
        pass


class TestDeleteConnection:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_delete_connection_success(self):
        pass


class TestDeleteTeam:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_delete_team_success(self):
        pass

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_delete_team_failure_sets_disconnected(self):
        pass


class TestUpdateConnection:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_update_connection_success(self):
        pass

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_update_connection_failure_sets_disconnected(self):
        pass


class TestSyncConnections:
    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_sync_creates_teams_for_enabled_orgs(self):
        pass

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_sync_skips_disabled_orgs(self):
        pass
