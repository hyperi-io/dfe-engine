#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_registry.py
#  Purpose:      Tests for ConnectionRegistry — connection resolution logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ConnectionRegistry.

Does NOT create real ClickHouse clients -- covers the unknown-name refusal only.
"""

import pytest

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.registry import ConnectionRegistry


def _make_config() -> ConnectionConfig:
    """Build a test ConnectionConfig with the default connection."""
    return ConnectionConfig(
        connections={
            "default": ClickHouseConnection(
                name="default",
                host="ch-default",
                user="default",
                password_env="CH_DEFAULT_PW",
            ),
        },
    )


class TestGetClient:
    """Test get_client raises on unknown connection."""

    def test_unknown_connection_raises_key_error(self) -> None:
        registry = ConnectionRegistry(_make_config())
        with pytest.raises(KeyError, match="nonexistent"):
            registry.get_client("nonexistent")
