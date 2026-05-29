#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_models.py
#  Purpose:      Tests for ClickHouseConnection model validation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ClickHouseConnection and ConnectionConfig model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection


class TestClickHouseConnection:
    """Test ClickHouseConnection Pydantic model."""

    def test_create_with_all_fields(self) -> None:
        conn = ClickHouseConnection(
            name="prod",
            host="ch-prod.internal",
            port=9000,
            database="analytics",
            user="analyst",
            password_env="CH_ANALYST_PW",
        )
        assert conn.name == "prod"
        assert conn.host == "ch-prod.internal"
        assert conn.port == 9000
        assert conn.database == "analytics"
        assert conn.user == "analyst"
        assert conn.password_env == "CH_ANALYST_PW"

    def test_create_with_defaults(self) -> None:
        conn = ClickHouseConnection(name="minimal")
        assert conn.host == "localhost"
        assert conn.port == 8123
        assert conn.database == "dfe"
        assert conn.user == "default"
        assert conn.password_env == ""

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            ClickHouseConnection()  # type: ignore[call-arg]

    def test_serialization_round_trip(self) -> None:
        conn = ClickHouseConnection(name="test", host="ch.example.com", port=9000)
        data = conn.model_dump()
        restored = ClickHouseConnection(**data)
        assert restored == conn


class TestConnectionConfig:
    """Test ConnectionConfig model."""

    def test_empty_config(self) -> None:
        config = ConnectionConfig()
        assert config.connections == {}
        assert config.role_connections == {}

    def test_config_with_connections(self) -> None:
        config = ConnectionConfig(
            connections={
                "default": ClickHouseConnection(name="default"),
                "tenant": ClickHouseConnection(name="tenant", user="tenant_user"),
            },
            role_connections={"admin": "default", "customer_viewer": "tenant"},
        )
        assert len(config.connections) == 2
        assert config.role_connections["admin"] == "default"
        assert config.connections["tenant"].user == "tenant_user"
