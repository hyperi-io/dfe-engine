#  Project:      dfe-engine
#  File:         settings.py
#  Purpose:      Centralized configuration management with Pydantic
#  Language:     Python
#
#  License:      LicenseRef-HyperSec-EULA
#  Copyright:    (c) 2025 HyperI

"""
DFE Engine Settings Module

Provides centralized configuration management using hyperi-pylib settings cascade.
Configuration priority: Environment Variables > Config Files > Defaults

Environment variable mapping (DFE_ prefixed, with legacy fallbacks):

ClickHouse:
- DFE_CLICKHOUSE_HOST (legacy: CLICKHOUSE_HOST) -> clickhouse.host
- DFE_CLICKHOUSE_PORT (legacy: CLICKHOUSE_PORT) -> clickhouse.port
- DFE_CLICKHOUSE_USERNAME (legacy: CLICKHOUSE_USER) -> clickhouse.username
- DFE_CLICKHOUSE_PASSWORD (legacy: CLICKHOUSE_PASSWORD) -> clickhouse.password
- DFE_CLICKHOUSE_DATABASE (legacy: CLICKHOUSE_DATABASE) -> clickhouse.database
- DFE_CLICKHOUSE_SECURE (legacy: CLICKHOUSE_SECURE) -> clickhouse.secure (true/false)
- DFE_CLICKHOUSE_VERIFY (legacy: CLICKHOUSE_VERIFY) -> clickhouse.verify (true/false)
- DFE_CLICKHOUSE_CONNECTIONS_MIN -> clickhouse.connections_min
- DFE_CLICKHOUSE_CONNECTIONS_MAX -> clickhouse.connections_max

Hunts:
- DFE_HUNT_LOG_PATH -> hunts.log_path

Artifactory:
- DFE_ARTIFACTORY_URL -> artifactory.url
- DFE_ARTIFACTORY_USERNAME -> artifactory.username
- DFE_ARTIFACTORY_PASSWORD -> artifactory.password
- DFE_TEMPLATES_VERSION -> artifactory.templates_version

Postgres:
- DFE_POSTGRES_HOST (legacy: POSTGRES_HOST) -> postgres.host
- DFE_POSTGRES_PORT (legacy: POSTGRES_PORT) -> postgres.port
- DFE_POSTGRES_USER (legacy: POSTGRES_USER) -> postgres.username
- DFE_POSTGRES_PASSWORD (legacy: POSTGRES_PASSWORD) -> postgres.password
- DFE_POSTGRES_DATABASE (legacy: POSTGRES_DATABASE) -> postgres.database

Kafka:
- DFE_KAFKA_BOOTSTRAP_SERVERS (legacy: KAFKA_BOOTSTRAP_SERVERS) -> kafka.bootstrap_servers
- DFE_KAFKA_SECURITY_PROTOCOL (legacy: KAFKA_SECURITY_PROTOCOL) -> kafka.security_protocol

Storage:
- DFE_STORAGE_TYPE -> storage.type (local, s3, http - auto-detected from path if not set)
- DFE_STORAGE_PATH -> storage.path (local path, S3 URI, or HTTP URL)
- DFE_S3_BUCKET -> storage.s3_bucket
- DFE_S3_REGION -> storage.s3_region
"""

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .yaml_utils import yaml_load


def _get_env(primary: str, *fallbacks: str) -> Optional[str]:
    """Get env var with fallback support. Primary (DFE_*) takes precedence over legacy names."""
    if val := os.getenv(primary):
        return val
    for fallback in fallbacks:
        if val := os.getenv(fallback):
            return val
    return None


class ClickHouseSettings(BaseModel):
    """ClickHouse connection settings."""

    host: str = Field(default="localhost")
    port: int = Field(default=9000)
    username: str = Field(default="default")
    password: str = Field(default="")
    database: str = Field(default="default")
    secure: bool = Field(default=True)
    verify: bool = Field(default=False)
    connections_min: int = Field(default=10)
    connections_max: int = Field(default=300)


class HuntsSettings(BaseModel):
    """Hunt scheduler settings."""

    log_path: str = Field(default="hunt_log_path")
    checkpoint_path: str = Field(default="")
    cron_task_timeout: int = Field(default=300)


class ArtifactorySettings(BaseModel):
    """Artifactory settings for downloading templates."""

    url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    templates_version: str = Field(default="latest")


class PostgresSettings(BaseModel):
    """PostgreSQL connection settings."""

    host: str = Field(default="localhost")
    port: int = Field(default=5432)
    username: str = Field(default="postgres")
    password: str = Field(default="")
    database: str = Field(default="dfe_engine")


class KafkaSettings(BaseModel):
    """Kafka connection settings."""

    bootstrap_servers: str = Field(default="localhost:9092")
    security_protocol: str = Field(default="PLAINTEXT")


class StorageSettings(BaseModel):
    """
    Storage backend settings for artifacts and templates.

    Supports local filesystem, S3, and HTTP backends.
    The 'type' field is auto-detected from 'path' if not explicitly set:
    - Paths starting with 's3://' -> S3 backend
    - Paths starting with 'http://' or 'https://' -> HTTP backend
    - All other paths -> Local filesystem backend
    """

    type: str = Field(default="auto", description="Storage type: auto, local, s3, http")
    path: str = Field(default="./artifacts", description="Storage path/URL")
    s3_bucket: str = Field(default="", description="S3 bucket name (for s3 type)")
    s3_region: str = Field(default="", description="AWS region (for s3 type)")


class DFESettings(BaseModel):
    """Main DFE Engine settings container."""

    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    hunts: HuntsSettings = Field(default_factory=HuntsSettings)
    artifactory: ArtifactorySettings = Field(default_factory=ArtifactorySettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)


def _load_defaults() -> dict:
    """Load default configuration from defaults.yaml."""
    defaults_path = Path(__file__).parent / "defaults.yaml"
    if defaults_path.exists():
        return yaml_load(defaults_path) or {}
    return {}


def _get_env_overrides() -> dict:
    """Get configuration overrides from environment variables.

    DFE_ prefixed vars take precedence over legacy names.
    """
    overrides = {
        "clickhouse": {},
        "hunts": {},
        "artifactory": {},
        "postgres": {},
        "kafka": {},
        "storage": {},
    }

    # ClickHouse settings (DFE_ prefix with legacy fallbacks)
    if val := _get_env("DFE_CLICKHOUSE_HOST", "CLICKHOUSE_HOST"):
        overrides["clickhouse"]["host"] = val
    if val := _get_env("DFE_CLICKHOUSE_PORT", "CLICKHOUSE_PORT"):
        overrides["clickhouse"]["port"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_USERNAME", "CLICKHOUSE_USER"):
        overrides["clickhouse"]["username"] = val
    if val := _get_env("DFE_CLICKHOUSE_PASSWORD", "CLICKHOUSE_PASSWORD"):
        overrides["clickhouse"]["password"] = val
    if val := _get_env("DFE_CLICKHOUSE_DATABASE", "CLICKHOUSE_DATABASE"):
        overrides["clickhouse"]["database"] = val
    if val := _get_env("DFE_CLICKHOUSE_SECURE", "CLICKHOUSE_SECURE"):
        overrides["clickhouse"]["secure"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_VERIFY", "CLICKHOUSE_VERIFY"):
        overrides["clickhouse"]["verify"] = val.lower() in ("true", "1", "yes")
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MIN"):
        overrides["clickhouse"]["connections_min"] = int(val)
    if val := _get_env("DFE_CLICKHOUSE_CONNECTIONS_MAX"):
        overrides["clickhouse"]["connections_max"] = int(val)

    # Hunts settings
    if val := _get_env("DFE_HUNT_LOG_PATH", "HUNT_LOG_PATH"):
        overrides["hunts"]["log_path"] = val

    # Artifactory settings
    if val := _get_env("DFE_ARTIFACTORY_URL", "ARTIFACTORY_VECTOR_TEMPLATES"):
        overrides["artifactory"]["url"] = val
    if val := _get_env("DFE_ARTIFACTORY_USERNAME", "ARTIFACTORY_USERNAME"):
        overrides["artifactory"]["username"] = val
    if val := _get_env("DFE_ARTIFACTORY_PASSWORD", "ARTIFACTORY_PASSWORD"):
        overrides["artifactory"]["password"] = val
    if val := _get_env("DFE_TEMPLATES_VERSION", "TEMPLATES_VERSION"):
        overrides["artifactory"]["templates_version"] = val

    # PostgreSQL settings (DFE_ prefix with legacy fallbacks)
    if val := _get_env("DFE_POSTGRES_HOST", "POSTGRES_HOST"):
        overrides["postgres"]["host"] = val
    if val := _get_env("DFE_POSTGRES_PORT", "POSTGRES_PORT"):
        overrides["postgres"]["port"] = int(val)
    if val := _get_env("DFE_POSTGRES_USER", "POSTGRES_USER"):
        overrides["postgres"]["username"] = val
    if val := _get_env("DFE_POSTGRES_PASSWORD", "POSTGRES_PASSWORD"):
        overrides["postgres"]["password"] = val
    if val := _get_env("DFE_POSTGRES_DATABASE", "POSTGRES_DATABASE"):
        overrides["postgres"]["database"] = val

    # Kafka settings (DFE_ prefix with legacy fallbacks)
    if val := _get_env("DFE_KAFKA_BOOTSTRAP_SERVERS", "KAFKA_BOOTSTRAP_SERVERS"):
        overrides["kafka"]["bootstrap_servers"] = val
    if val := _get_env("DFE_KAFKA_SECURITY_PROTOCOL", "KAFKA_SECURITY_PROTOCOL"):
        overrides["kafka"]["security_protocol"] = val

    # Storage settings (for on-prem/Rancher deployments)
    if val := _get_env("DFE_STORAGE_TYPE"):
        overrides["storage"]["type"] = val
    if val := _get_env("DFE_STORAGE_PATH"):
        overrides["storage"]["path"] = val
    if val := _get_env("DFE_S3_BUCKET"):
        overrides["storage"]["s3_bucket"] = val
    if val := _get_env("DFE_S3_REGION"):
        overrides["storage"]["s3_region"] = val

    # Remove empty sections
    return {k: v for k, v in overrides.items() if v}


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries, with override taking precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_settings(config_file: Optional[str] = None) -> DFESettings:
    """
    Load DFE settings with the following precedence:
    1. Environment variables (highest priority)
    2. User config file (if provided)
    3. defaults.yaml (lowest priority)

    Args:
        config_file: Optional path to a YAML configuration file

    Returns:
        DFESettings: Validated settings object
    """
    # Start with defaults
    config = _load_defaults()

    # Merge user config file if provided
    if config_file:
        config_path = Path(config_file)
        if config_path.exists():
            user_config = yaml_load(config_path) or {}
            config = _deep_merge(config, user_config)

    # Apply environment variable overrides
    env_overrides = _get_env_overrides()
    config = _deep_merge(config, env_overrides)

    return DFESettings(**config)


def get_clickhouse_config(settings: Optional[DFESettings] = None) -> dict:
    """
    Get ClickHouse configuration as a dictionary.

    This provides backward compatibility with existing code that expects
    a dictionary with keys like 'ch_host', 'ch_port', etc.

    Args:
        settings: Optional DFESettings object. If not provided, loads settings.

    Returns:
        dict: ClickHouse configuration dictionary
    """
    if settings is None:
        settings = load_settings()

    return {
        "ch_host": settings.clickhouse.host,
        "ch_port": settings.clickhouse.port,
        "ch_username": settings.clickhouse.username,
        "ch_password": settings.clickhouse.password,
        "ch_database": settings.clickhouse.database,
        "ch_secure": settings.clickhouse.secure,
        "ch_verify": settings.clickhouse.verify,
    }


# Global settings instance (lazy loaded)
_settings: Optional[DFESettings] = None


def get_settings() -> DFESettings:
    """Get the global settings instance, loading if necessary."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Reset the global settings instance (useful for testing)."""
    global _settings
    _settings = None
