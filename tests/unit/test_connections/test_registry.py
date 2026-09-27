#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_registry.py
#  Purpose:      Tests for ConnectionRegistry — connection resolution logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ConnectionRegistry.

Does NOT create real ClickHouse clients -- covers listing and the
unknown-name refusal only.
"""

import pytest

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
    )


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
