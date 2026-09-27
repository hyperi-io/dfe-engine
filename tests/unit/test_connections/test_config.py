#  Project:      dfe-engine
#  File:         tests/unit/test_connections/test_config.py
#  Purpose:      Tests for connection configuration loading from YAML
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for ConnectionConfigLoader — YAML loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.connections.config import ConnectionConfigLoader


class TestConnectionConfigLoader:
    """Test loading connection config from YAML files."""

    def test_load_default_returns_valid_config(self) -> None:
        config = ConnectionConfigLoader.load_default()
        assert set(config.connections) == {"default"}
        assert config.connections["default"].host == "localhost"
        assert config.connections["default"].port == 8123
        assert config.connections["default"].database == "dfe"
        assert config.connections["default"].user == "default"
        assert config.connections["default"].password_env == "CH_DEFAULT_PASSWORD"

    def test_load_from_file(self, tmp_path: Path) -> None:
        yaml_content = """\
connections:
  myconn:
    host: ch.prod.internal
    port: 9000
    database: prod_db
    user: prod_user
    password_env: PROD_PW
"""
        config_file = tmp_path / "connections.yaml"
        config_file.write_text(yaml_content)

        config = ConnectionConfigLoader.load(config_file)
        assert "myconn" in config.connections
        assert config.connections["myconn"].host == "ch.prod.internal"
        assert config.connections["myconn"].port == 9000
        assert config.connections["myconn"].name == "myconn"

    def test_a_file_that_still_maps_roles_loads(self, tmp_path: Path) -> None:
        """A deployer's file written for the retired role map still loads its connections."""
        config_file = tmp_path / "connections.yaml"
        config_file.write_text(
            "connections:\n  x:\n    host: localhost\nrole_connections:\n  admin: x\n"
        )

        config = ConnectionConfigLoader.load(config_file)
        assert set(config.connections) == {"x"}

    def test_load_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ConnectionConfigLoader.load(tmp_path / "nonexistent.yaml")

    def test_load_invalid_connections_type(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("connections: not_a_dict\n")

        with pytest.raises(ValueError, match="'connections' must be a mapping"):
            ConnectionConfigLoader.load(config_file)

    def test_load_invalid_connection_entry(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("connections:\n  bad_conn: just_a_string\n")

        with pytest.raises(ValueError, match="Connection 'bad_conn' must be a mapping"):
            ConnectionConfigLoader.load(config_file)

    def test_load_empty_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        with pytest.raises(ValueError, match="must be a YAML mapping"):
            ConnectionConfigLoader.load(config_file)

    def test_load_minimal_valid(self, tmp_path: Path) -> None:
        config_file = tmp_path / "minimal.yaml"
        config_file.write_text("connections: {}\n")

        config = ConnectionConfigLoader.load(config_file)
        assert config.connections == {}

    def test_load_connection_inherits_name_from_key(self, tmp_path: Path) -> None:
        yaml_content = """\
connections:
  analytics:
    host: ch-analytics.internal
"""
        config_file = tmp_path / "named.yaml"
        config_file.write_text(yaml_content)

        config = ConnectionConfigLoader.load(config_file)
        assert config.connections["analytics"].name == "analytics"
