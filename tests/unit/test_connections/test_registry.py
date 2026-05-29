#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_registry.py
#  Purpose:      Tests for ConnectionRegistry — connection resolution logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ConnectionRegistry.

Focus: connection name resolution and role mapping.  Does NOT create
real ClickHouse clients — tests the resolution logic only.
"""

from __future__ import annotations

import pytest

from dfe_engine.auth.models import AuthContext
from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.registry import ConnectionRegistry


def _make_config() -> ConnectionConfig:
    """Build a test ConnectionConfig with default + tenant_reader connections."""
    return ConnectionConfig(
        connections={
            "default": ClickHouseConnection(
                name="default",
                host="ch-default",
                user="default",
                password_env="CH_DEFAULT_PW",
            ),
            "tenant_reader": ClickHouseConnection(
                name="tenant_reader",
                host="ch-tenant",
                user="dfe_tenant_reader",
                password_env="CH_TENANT_PW",
            ),
        },
        role_connections={
            "admin": "default",
            "data_analyst": "default",
            "data_analyst_viewer": "default",
            "data_viewer": "default",
            "infra_admin": "default",
            "infra_viewer": "default",
            "customer_viewer": "tenant_reader",
        },
    )


class TestConnectionNameResolution:
    """Test get_connection_name role-to-connection mapping."""

    def test_admin_resolves_to_default(self) -> None:
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(user_id="alice", roles=["admin"])
        assert registry.get_connection_name(auth) == "default"

    def test_data_analyst_resolves_to_default(self) -> None:
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(user_id="bob", roles=["data_analyst"])
        assert registry.get_connection_name(auth) == "default"

    def test_customer_viewer_resolves_to_tenant_reader(self) -> None:
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(
            user_id="customer1",
            roles=["customer_viewer"],
            org_ids=["org-abc"],
        )
        assert registry.get_connection_name(auth) == "tenant_reader"

    def test_multiple_roles_highest_privilege_wins(self) -> None:
        registry = ConnectionRegistry(_make_config())
        # admin is higher precedence than customer_viewer
        auth = AuthContext(
            user_id="superuser",
            roles=["customer_viewer", "admin"],
        )
        assert registry.get_connection_name(auth) == "default"

    def test_infra_viewer_resolves_to_default(self) -> None:
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(user_id="ops", roles=["infra_viewer"])
        assert registry.get_connection_name(auth) == "default"

    def test_no_roles_falls_back_to_default(self) -> None:
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(user_id="anon", roles=[])
        assert registry.get_connection_name(auth) == "default"

    def test_unknown_role_falls_back_to_default(self) -> None:
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(user_id="unknown", roles=["custom_role_xyz"])
        assert registry.get_connection_name(auth) == "default"

    def test_non_precedence_role_with_mapping(self) -> None:
        """A role not in the precedence list but present in role_connections."""
        config = _make_config()
        config.role_connections["special_viewer"] = "tenant_reader"
        registry = ConnectionRegistry(config)
        auth = AuthContext(user_id="special", roles=["special_viewer"])
        assert registry.get_connection_name(auth) == "tenant_reader"

    def test_data_analyst_viewer_over_customer_viewer(self) -> None:
        """data_analyst_viewer has higher precedence than customer_viewer."""
        registry = ConnectionRegistry(_make_config())
        auth = AuthContext(
            user_id="mixed",
            roles=["customer_viewer", "data_analyst_viewer"],
        )
        assert registry.get_connection_name(auth) == "default"


class TestListConnections:
    """Test list_connections."""

    def test_list_returns_all_connections(self) -> None:
        registry = ConnectionRegistry(_make_config())
        connections = registry.list_connections()
        names = {c.name for c in connections}
        assert names == {"default", "tenant_reader"}

    def test_list_empty_config(self) -> None:
        registry = ConnectionRegistry(ConnectionConfig())
        assert registry.list_connections() == []


class TestGetClient:
    """Test get_client raises on unknown connection."""

    def test_unknown_connection_raises_key_error(self) -> None:
        registry = ConnectionRegistry(_make_config())
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get_client("nonexistent")
