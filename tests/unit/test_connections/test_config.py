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
        # the four fixed-user connections
        assert set(config.connections) == {"default", "analyst", "analyst_ro", "tenant_reader"}
        assert config.connections["default"].host == "localhost"
        assert config.connections["default"].port == 8123
        assert config.connections["default"].database == "dfe"
        # default = the CH superuser (dfe_admin identity), not minted
        assert config.connections["default"].user == "default"
        assert config.connections["default"].password_env == "CH_DEFAULT_PASSWORD"
        assert config.connections["analyst"].user == "dfe_analyst"
        assert config.connections["analyst_ro"].user == "dfe_analyst_ro"
        assert config.connections["tenant_reader"].user == "dfe_tenant_reader"

    def test_load_default_role_connections(self) -> None:
        config = ConnectionConfigLoader.load_default()
        assert config.role_connections["admin"] == "default"
        assert config.role_connections["infra"] == "default"
        assert config.role_connections["org_analyst"] == "tenant_reader"
        assert config.role_connections["data_analyst"] == "analyst"
        assert config.role_connections["data_analyst_ro"] == "analyst_ro"
        assert config.role_connections["data_viewer"] == "analyst_ro"

    def test_load_from_file(self, tmp_path: Path) -> None:
        yaml_content = """\
connections:
  myconn:
    host: ch.prod.internal
    port: 9000
    database: prod_db
    user: prod_user
    password_env: PROD_PW

role_connections:
  admin: myconn
"""
        config_file = tmp_path / "connections.yaml"
        config_file.write_text(yaml_content)

        config = ConnectionConfigLoader.load(config_file)
        assert "myconn" in config.connections
        assert config.connections["myconn"].host == "ch.prod.internal"
        assert config.connections["myconn"].port == 9000
        assert config.connections["myconn"].name == "myconn"
        assert config.role_connections["admin"] == "myconn"

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

    def test_load_invalid_role_connections_type(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.yaml"
        config_file.write_text(
            "connections:\n  x:\n    host: localhost\nrole_connections: not_a_dict\n"
        )

        with pytest.raises(ValueError, match="'role_connections' must be a mapping"):
            ConnectionConfigLoader.load(config_file)

    def test_load_empty_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")

        with pytest.raises(ValueError, match="must be a YAML mapping"):
            ConnectionConfigLoader.load(config_file)

    def test_load_minimal_valid(self, tmp_path: Path) -> None:
        config_file = tmp_path / "minimal.yaml"
        config_file.write_text("connections: {}\nrole_connections: {}\n")

        config = ConnectionConfigLoader.load(config_file)
        assert config.connections == {}
        assert config.role_connections == {}

    def test_load_connection_inherits_name_from_key(self, tmp_path: Path) -> None:
        yaml_content = """\
connections:
  analytics:
    host: ch-analytics.internal
role_connections: {}
"""
        config_file = tmp_path / "named.yaml"
        config_file.write_text(yaml_content)

        config = ConnectionConfigLoader.load(config_file)
        assert config.connections["analytics"].name == "analytics"
