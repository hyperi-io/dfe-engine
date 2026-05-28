#  Project:      dfe-engine
#  File:         connections/config.py
#  Purpose:      Load and validate connection configuration from YAML
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Connection configuration loader.

Reads ``connections.yaml`` (role-to-connection mapping + connection
definitions) and returns a validated ``ConnectionConfig``.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

from pydantic import BaseModel, Field

from dfe_engine.connections.models import ClickHouseConnection
from dfe_engine.yaml_utils import yaml_load, yaml_load_string


class ConnectionConfig(BaseModel):
    """Validated connection configuration.

    Attributes:
        connections: Named connection definitions.
        role_connections: Maps role names to connection names.
    """

    connections: dict[str, ClickHouseConnection] = Field(default_factory=dict)
    role_connections: dict[str, str] = Field(default_factory=dict)


class ConnectionConfigLoader:
    """Load connection config from YAML files or built-in defaults."""

    @staticmethod
    def load(path: Path) -> ConnectionConfig:
        """Load connection configuration from a YAML file.

        Args:
            path: Path to ``connections.yaml``.

        Returns:
            Validated ConnectionConfig.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the YAML structure is invalid.
        """
        data = yaml_load(path)
        return ConnectionConfigLoader._from_data(data)

    @staticmethod
    def load_default() -> ConnectionConfig:
        """Return the built-in default connection configuration.

        Reads from the package resource at
        ``dfe_engine/connections/resources/connections.yaml``.

        Returns:
            ConnectionConfig with default connections.
        """
        pkg = importlib.resources.files("dfe_engine.connections.resources")
        resource = pkg.joinpath("connections.yaml")
        content = resource.read_text(encoding="utf-8")
        data = yaml_load_string(content)
        return ConnectionConfigLoader._from_data(data)

    @staticmethod
    def _from_data(data: dict) -> ConnectionConfig:
        """Parse raw YAML dict into a ConnectionConfig.

        Args:
            data: Parsed YAML dictionary.

        Returns:
            Validated ConnectionConfig.

        Raises:
            ValueError: If the data is missing required keys.
        """
        if not isinstance(data, dict):
            raise ValueError("Connection config must be a YAML mapping")

        raw_connections = data.get("connections", {})
        if not isinstance(raw_connections, dict):
            raise ValueError("'connections' must be a mapping")

        connections: dict[str, ClickHouseConnection] = {}
        for name, raw in raw_connections.items():
            if isinstance(raw, dict):
                connections[name] = ClickHouseConnection(name=name, **raw)
            else:
                raise ValueError(f"Connection '{name}' must be a mapping")

        role_connections = data.get("role_connections", {})
        if not isinstance(role_connections, dict):
            raise ValueError("'role_connections' must be a mapping")

        return ConnectionConfig(
            connections=connections,
            role_connections=role_connections,
        )
