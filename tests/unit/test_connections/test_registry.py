#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_registry.py
#  Purpose:      Tests for ConnectionRegistry — connection resolution logic
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ConnectionRegistry.

Patches ``clickhouse_connect.get_client`` to capture kwargs instead of
connecting -- covers the unknown-name refusal and the TLS posture every
named connection takes from the engine's ClickHouseSettings.
"""

from unittest.mock import patch

import pytest

from dfe_engine.connections.config import ConnectionConfig
from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.connections.registry import ConnectionRegistry
from dfe_engine.settings import ClickHouseSettings


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


class TestGetClientTls:
    """Every named connection takes the engine's ClickHouseSettings TLS posture."""

    def test_insecure_settings_pass_no_tls_kwargs(self) -> None:
        ch = ClickHouseSettings(secure=False)
        registry = ConnectionRegistry(_make_config(), ch)
        with patch("clickhouse_connect.get_client") as get_client:
            registry.get_client("default")
        kwargs = get_client.call_args[1]
        assert "secure" not in kwargs
        assert "verify" not in kwargs
        assert "ca_cert" not in kwargs

    def test_secure_settings_pass_verify_and_ca_cert(self, tmp_path) -> None:
        ca = tmp_path / "internal-ca.pem"
        ca.write_text("cert")
        ch = ClickHouseSettings(secure=True, verify=True, ca_cert=str(ca))
        registry = ConnectionRegistry(_make_config(), ch)
        with patch("clickhouse_connect.get_client") as get_client:
            registry.get_client("default")
        kwargs = get_client.call_args[1]
        assert kwargs["secure"] is True
        assert kwargs["verify"] is True
        assert kwargs["ca_cert"] == str(ca)

    def test_falls_back_to_get_settings_when_none_given(self, monkeypatch) -> None:
        from dfe_engine import settings as settings_module

        monkeypatch.setattr(
            settings_module,
            "get_settings",
            lambda: settings_module.DFESettings(env="test"),
        )
        registry = ConnectionRegistry(_make_config())
        with patch("clickhouse_connect.get_client") as get_client:
            registry.get_client("default")
        get_client.assert_called_once()
