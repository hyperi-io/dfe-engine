#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_registry.py
#  Purpose:      Tests for ConnectionRegistry - resolution + alias-awareness
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ConnectionRegistry.

Focus: role -> fixed-user connection resolution, precedence, and alias-awareness.
Does NOT create real ClickHouse clients - tests the resolution logic only.
"""

from __future__ import annotations

import pytest

from dfe_engine.auth.models import AuthContext
from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.registry import ConnectionRegistry
from dfe_engine.connections.tenant import TenantScopedClient

_TENANT_SETTING = "DFE_current_tenant_id"


def _conn(name: str, user: str, pw_env: str) -> ClickHouseConnection:
    return ClickHouseConnection(name=name, host="ch", user=user, password_env=pw_env)


def _make_config() -> ConnectionConfig:
    """The fixed-user connection map (mirrors resources/connections.yaml)."""
    return ConnectionConfig(
        connections={
            "default": _conn("default", "default", "CH_DEFAULT_PASSWORD"),
            "analyst": _conn("analyst", "dfe_analyst", "CH_ANALYST_PASSWORD"),
            "analyst_ro": _conn("analyst_ro", "dfe_analyst_ro", "CH_ANALYST_RO_PASSWORD"),
            "tenant_reader": _conn(
                "tenant_reader", "dfe_tenant_reader", "CH_TENANT_READER_PASSWORD"
            ),
        },
        role_connections={
            "admin": "default",
            "infra": "default",
            "data_analyst": "analyst",
            "data_analyst_ro": "analyst_ro",
            "data_viewer": "analyst_ro",
            "infra_ro": "analyst_ro",
            "org_analyst": "tenant_reader",
        },
    )


def _name(roles: list[str], **kw) -> str:
    auth = AuthContext(user_id="u", roles=roles, **kw)
    return ConnectionRegistry(_make_config()).get_connection_name(auth)


class TestConnectionNameResolution:
    """get_connection_name role-to-connection mapping (fixed users)."""

    def test_admin_resolves_to_default(self) -> None:
        assert _name(["admin"]) == "default"

    def test_infra_resolves_to_default(self) -> None:
        assert _name(["infra"]) == "default"

    def test_data_analyst_resolves_to_analyst(self) -> None:
        assert _name(["data_analyst"]) == "analyst"

    def test_data_analyst_ro_resolves_to_analyst_ro(self) -> None:
        assert _name(["data_analyst_ro"]) == "analyst_ro"

    def test_data_viewer_resolves_to_analyst_ro(self) -> None:
        assert _name(["data_viewer"]) == "analyst_ro"

    def test_infra_ro_resolves_to_analyst_ro(self) -> None:
        assert _name(["infra_ro"]) == "analyst_ro"

    def test_org_analyst_resolves_to_tenant_reader(self) -> None:
        assert _name(["org_analyst"], org_ids=["org-abc"]) == "tenant_reader"

    def test_multiple_roles_highest_privilege_wins(self) -> None:
        # admin is higher precedence than org_analyst.
        assert _name(["org_analyst", "admin"]) == "default"

    def test_data_analyst_ro_over_org_analyst(self) -> None:
        # data_analyst_ro has higher precedence than org_analyst.
        assert _name(["org_analyst", "data_analyst_ro"]) == "analyst_ro"

    def test_no_roles_fails_closed_to_most_restricted(self) -> None:
        # Fail-CLOSED: no roles must NOT land on the admin `default` connection -
        # it resolves to the most-restricted (tenant_reader, row-filtered).
        assert _name([]) == "tenant_reader"

    def test_unknown_role_fails_closed_to_most_restricted(self) -> None:
        assert _name(["custom_role_xyz"]) == "tenant_reader"

    def test_non_precedence_role_with_mapping(self) -> None:
        """A role not in the precedence list but present in role_connections."""
        config = _make_config()
        config.role_connections["special_viewer"] = "tenant_reader"
        auth = AuthContext(user_id="special", roles=["special_viewer"])
        assert ConnectionRegistry(config).get_connection_name(auth) == "tenant_reader"


class TestAliasAwareResolution:
    """A STALE pre-rename role must resolve through ROLE_ALIASES to its canonical
    connection, NEVER fall through to the admin `default` fallback (phase-1 gap)."""

    def test_stale_customer_viewer_maps_to_tenant_reader(self) -> None:
        # customer_viewer -> org_analyst -> tenant_reader (row-filtered), NOT default.
        assert _name(["customer_viewer"]) == "tenant_reader"

    def test_stale_customer_viewer_not_over_privileged_to_admin(self) -> None:
        # The exact bug this closes: a lone stale customer_viewer must not land on
        # the admin `default` connection.
        assert _name(["customer_viewer"]) != "default"

    def test_stale_data_analyst_viewer_maps_to_analyst_ro(self) -> None:
        assert _name(["data_analyst_viewer"]) == "analyst_ro"

    def test_stale_infra_admin_maps_to_default(self) -> None:
        assert _name(["infra_admin"]) == "default"

    def test_stale_infra_viewer_maps_to_analyst_ro(self) -> None:
        assert _name(["infra_viewer"]) == "analyst_ro"

    def test_alias_and_canonical_together_pick_highest(self) -> None:
        # A stale customer_viewer alongside admin still yields admin's connection.
        assert _name(["customer_viewer", "admin"]) == "default"


class TestListConnections:
    """list_connections."""

    def test_list_returns_all_connections(self) -> None:
        names = {c.name for c in ConnectionRegistry(_make_config()).list_connections()}
        assert names == {"default", "analyst", "analyst_ro", "tenant_reader"}

    def test_list_empty_config(self) -> None:
        assert ConnectionRegistry(ConnectionConfig()).list_connections() == []


class TestGetClient:
    """get_client raises on unknown connection."""

    def test_unknown_connection_raises_key_error(self) -> None:
        registry = ConnectionRegistry(_make_config())
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get_client("nonexistent")

    def test_host_port_seeded_from_settings_override(self, monkeypatch) -> None:
        # ch_host/ch_port override every connection's configured host/port at
        # client creation (all fixed users share ONE cluster); only the USER
        # differs. Assert the seeded values reach clickhouse_connect.get_client.
        captured: dict = {}

        class _Client:
            pass

        def _fake_get_client(**kwargs):
            captured.update(kwargs)
            return _Client()

        import clickhouse_connect

        monkeypatch.setattr(clickhouse_connect, "get_client", _fake_get_client)
        registry = ConnectionRegistry(_make_config(), ch_host="ch.internal", ch_port=8443)
        registry.get_client("tenant_reader")
        assert captured["host"] == "ch.internal"
        assert captured["port"] == 8443
        assert captured["username"] == "dfe_tenant_reader"  # user is NOT overridden


class _RecordingClient:
    """A pre-seeded stand-in for a per-connection clickhouse-connect client.

    Records the CH user it represents and the settings of the last query so a test
    can assert BOTH which fixed user a principal resolved to AND what per-query
    settings (the tenant id) were injected - without any real ClickHouse.
    """

    def __init__(self, user: str) -> None:
        self.user = user
        self.last_settings: dict | None = None

    def query(self, sql: str, parameters=None, settings=None):
        self.last_settings = settings
        return "ok"


def _registry_with_clients() -> tuple[ConnectionRegistry, dict[str, _RecordingClient]]:
    """A registry whose client cache is pre-seeded, so get_client never connects."""
    registry = ConnectionRegistry(_make_config())
    clients = {
        "default": _RecordingClient("dfe_admin"),
        "analyst": _RecordingClient("dfe_analyst"),
        "analyst_ro": _RecordingClient("dfe_analyst_ro"),
        "tenant_reader": _RecordingClient("dfe_tenant_reader"),
    }
    registry._clients.update(clients)
    return registry, clients


def _auth(roles: list[str], **kw) -> AuthContext:
    return AuthContext(user_id="u", roles=roles, **kw)


class TestReadClientForUser:
    """read_client_for_user: privilege-appropriate wrapping for direct-CH reads."""

    def test_org_analyst_gets_tenant_scoped_client_with_org_ids(self) -> None:
        registry, clients = _registry_with_clients()
        client = registry.read_client_for_user(_auth(["org_analyst"], org_ids=["acme"]))

        assert isinstance(client, TenantScopedClient)
        # wraps the row-filtered dfe_tenant_reader, not the admin default
        assert client._client is clients["tenant_reader"]
        client.query("SELECT 1")
        assert clients["tenant_reader"].last_settings == {_TENANT_SETTING: "acme"}

    def test_org_analyst_multi_org_comma_joined(self) -> None:
        registry, clients = _registry_with_clients()
        client = registry.read_client_for_user(_auth(["org_analyst"], org_ids=["acme", "globex"]))
        client.query("SELECT 1")
        assert clients["tenant_reader"].last_settings == {_TENANT_SETTING: "acme,globex"}

    def test_org_analyst_no_org_ids_fails_closed_to_empty(self) -> None:
        # FAIL CLOSED: no org_ids -> '' (matches no _org_id -> zero rows), still
        # injected (never omitted) so a pooled reader session can't leak a stale id.
        registry, clients = _registry_with_clients()
        client = registry.read_client_for_user(_auth(["org_analyst"], org_ids=[]))
        assert isinstance(client, TenantScopedClient)
        client.query("SELECT 1")
        assert clients["tenant_reader"].last_settings == {_TENANT_SETTING: ""}

    def test_admin_gets_unwrapped_default_client_no_tenant_setting(self) -> None:
        registry, clients = _registry_with_clients()
        client = registry.read_client_for_user(_auth(["admin"]))

        # dfe_admin is targeted by no row policy -> the raw client, no wrapper.
        assert client is clients["default"]
        assert not isinstance(client, TenantScopedClient)
        client.query("SELECT 1", settings={"max_execution_time": 30})
        assert clients["default"].last_settings == {"max_execution_time": 30}

    def test_data_analyst_gets_unwrapped_analyst_client(self) -> None:
        registry, clients = _registry_with_clients()
        client = registry.read_client_for_user(_auth(["data_analyst"], org_ids=["acme"]))
        # data_analyst is read-write, not a policy target -> analyst client as-is,
        # NOT tenant-scoped even though the principal carries org_ids.
        assert client is clients["analyst"]

    def test_data_viewer_gets_readonly_wrapper_without_tenant_setting(self) -> None:
        registry, clients = _registry_with_clients()
        client = registry.read_client_for_user(_auth(["data_viewer"]))

        # dfe_analyst_ro: wrapped (readonly strip) but NOT tenant-filtered.
        assert isinstance(client, TenantScopedClient)
        assert client._client is clients["analyst_ro"]
        assert client.readonly is True
        client.query("SELECT 1", settings={"max_execution_time": 30})
        # readonly drops the rejected setting and injects NO tenant id
        assert clients["analyst_ro"].last_settings == {}
