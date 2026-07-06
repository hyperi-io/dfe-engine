#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_client.py
#  Purpose:      Tests for HyperDXClient (pure logic, no HTTP)
#  Language:     Python
#
#  License:      BUSL-1.1
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
from dfe_engine.hyperdx.client import (
    HYPERDX_CONNECTIONS_PATH,
    HYPERDX_SOURCES_PATH,
    HyperDXClient,
    build_hyperdx_connections_json,
    build_hyperdx_sources_json,
)

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


class TestInviteMember:
    @pytest.mark.asyncio
    async def test_invite_member_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.invite_member(team_api_key="team-key", email="user@corp.com")
        assert result is False

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_invite_member_success(self):
        pass


class TestGetTeamApiKey:
    @pytest.mark.asyncio
    async def test_get_team_api_key_returns_none_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.get_team_api_key(team_id="t1")
        assert result is None

    @pytest.mark.skip(reason="Requires running HyperDX instance")
    async def test_get_team_api_key_success(self):
        pass


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


class TestRemoveMember:
    """remove_member is the propagation counterpart to invite_member (5c.4)."""

    @pytest.mark.asyncio
    async def test_remove_member_returns_false_when_disconnected(self):
        client = HyperDXClient(base_url="http://x", api_key="k")
        client._connected = False
        result = await client.remove_member(team_api_key="team-key", email="user@corp.com")
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_member_does_not_latch_client_disconnected(self):
        # Team-scoped like invite_member: a per-team API-key failure must NOT mark
        # the whole client unreachable (mark_disconnected=False). base_url is
        # unroutable so the request fails, but _connected stays True.
        client = HyperDXClient(base_url="http://127.0.0.1:1", api_key="k")
        result = await client.remove_member(team_api_key="team-key", email="user@corp.com")
        assert result is False
        assert client._connected is True


# ---------------------------------------------------------------------------
# build_hyperdx_connections_json (per-ORG DEFAULT_CONNECTIONS, tenant-reader model)
# ---------------------------------------------------------------------------


class TestBuildHyperDXConnectionsJson:
    """One CH connection per org: shared dfe_tenant_reader + baked-in tenant setting."""

    def _base(self) -> ClickHouseConnection:
        return ClickHouseConnection(
            name="default", host="ch", port=8123, database="dfe", user="default"
        )

    def _secrets(self, tmp_path):
        from dfe_engine.secrets import build_secrets
        from dfe_engine.settings import SecretsSettings

        return build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))

    def test_connection_uses_tenant_reader_and_org_tenant_setting(self, tmp_path):
        """Task A: an org connection authenticates as the SHARED dfe_tenant_reader and
        carries DFE_current_tenant_id = the org's comma-joined org_ids."""
        from types import SimpleNamespace

        secrets = self._secrets(tmp_path)
        secrets.put("ch/fixed/dfe_tenant_reader", "reader-pw")  # reconciler stores it here

        orgs = [SimpleNamespace(name="acme", org_ids=["acme", "globex"])]
        result = json.loads(
            build_hyperdx_connections_json(orgs, base=self._base(), secrets_store=secrets)
        )
        assert len(result) == 1
        conn = result[0]
        assert conn["name"] == "acme"
        assert conn["user"] == "dfe_tenant_reader"  # shared fixed reader, NOT dfe_grp_*
        assert conn["password"] == "reader-pw"  # sourced from ch/fixed/dfe_tenant_reader
        assert conn["clickhouseSettings"] == {"DFE_current_tenant_id": "acme,globex"}
        assert conn["host"] == "ch"
        assert conn["port"] == 8123
        assert conn["database"] == "dfe"

    def test_empty_org_ids_fails_closed_with_empty_setting(self, tmp_path):
        """An org with no org_ids -> empty tenant setting -> row policy yields 0 rows."""
        from types import SimpleNamespace

        orgs = [SimpleNamespace(name="acme", org_ids=[])]
        result = json.loads(
            build_hyperdx_connections_json(
                orgs, base=self._base(), secrets_store=self._secrets(tmp_path)
            )
        )
        assert result[0]["clickhouseSettings"] == {"DFE_current_tenant_id": ""}

    def test_disabled_org_skipped(self, tmp_path):
        from types import SimpleNamespace

        orgs = [
            SimpleNamespace(name="acme", org_ids=["acme"], enabled=True),
            SimpleNamespace(name="dormant", org_ids=["dormant"], enabled=False),
        ]
        result = json.loads(
            build_hyperdx_connections_json(
                orgs, base=self._base(), secrets_store=self._secrets(tmp_path)
            )
        )
        assert [c["name"] for c in result] == ["acme"]

    def test_missing_secret_yields_empty_password(self, tmp_path):
        from types import SimpleNamespace

        orgs = [SimpleNamespace(name="acme", org_ids=["acme"])]  # no secret seeded
        result = json.loads(
            build_hyperdx_connections_json(
                orgs, base=self._base(), secrets_store=self._secrets(tmp_path)
            )
        )
        assert len(result) == 1
        assert result[0]["user"] == "dfe_tenant_reader"
        assert result[0]["password"] == ""  # non-fatal: password lands next publish

    def test_no_orgs_yields_empty_array(self, tmp_path):
        assert (
            build_hyperdx_connections_json(
                [], base=self._base(), secrets_store=self._secrets(tmp_path)
            )
            == "[]"
        )


# ---------------------------------------------------------------------------
# build_hyperdx_sources_json (DEFAULT_SOURCES, Task F)
# ---------------------------------------------------------------------------


class TestBuildHyperDXSourcesJson:
    """One HyperDX `log` source per built source table, fixed DFE landing expressions."""

    def test_one_log_source_per_table(self):
        result = json.loads(build_hyperdx_sources_json([("dfe", "filebeat")], connection="acme"))
        assert len(result) == 1
        src = result[0]
        assert src["name"] == "dfe.filebeat"
        assert src["kind"] == "log"
        assert src["connection"] == "acme"
        assert src["from"] == {"databaseName": "dfe", "tableName": "filebeat"}
        assert src["timestampValueExpression"] == "_timestamp_load"
        assert src["defaultTableSelectExpression"] == "_timestamp_load, _json"
        assert src["bodyExpression"] == "_json"

    def test_multiple_tables(self):
        result = json.loads(
            build_hyperdx_sources_json(
                [("dfe", "filebeat"), ("dfe", "crowdstrike")], connection="acme"
            )
        )
        assert [s["name"] for s in result] == ["dfe.filebeat", "dfe.crowdstrike"]

    def test_empty_yields_empty_array(self):
        assert build_hyperdx_sources_json([], connection="acme") == "[]"

    def test_idempotent_by_name(self):
        """Re-emitting an unchanged set is byte-identical (gitops publish no-op)."""
        a = build_hyperdx_sources_json([("dfe", "filebeat")], connection="acme")
        b = build_hyperdx_sources_json([("dfe", "filebeat")], connection="acme")
        assert a == b


def test_hyperdx_connections_path_is_stable():
    assert HYPERDX_CONNECTIONS_PATH == "hyperdx/connections.json"


def test_hyperdx_sources_path_is_stable():
    assert HYPERDX_SOURCES_PATH == "hyperdx/sources.json"
