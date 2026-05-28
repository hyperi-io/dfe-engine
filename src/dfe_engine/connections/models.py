#  Project:      dfe-engine
#  File:         connections/models.py
#  Purpose:      Pydantic models for ClickHouse connection definitions
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pydantic models for ClickHouse connection configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClickHouseConnection(BaseModel):
    """A named ClickHouse connection definition.

    Passwords are never stored in YAML.  Instead, ``password_env``
    names the environment variable that holds the password at runtime.

    Attributes:
        name: Logical name of this connection (e.g. "default", "tenant_reader").
        host: ClickHouse server hostname.
        port: ClickHouse HTTP port.
        database: Default database.
        user: ClickHouse username.
        password_env: Name of the environment variable holding the password.
    """

    name: str = Field(..., description="Logical connection name")
    host: str = Field(default="localhost", description="ClickHouse server hostname")
    port: int = Field(default=8123, description="ClickHouse HTTP port")
    database: str = Field(default="dfe", description="Default database")
    user: str = Field(default="default", description="ClickHouse username")
    password_env: str = Field(
        default="",
        description="Environment variable name holding the password",
    )
