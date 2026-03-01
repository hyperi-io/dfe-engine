"""DFEConfigLoader — compatibility shim.

Provides the same static-method interface as the original
deprecated/config/config_loader.py, but delegates to
``dfe_engine.settings.get_settings()`` and ``yaml_utils``.

Callers that only need ClickHouse settings should migrate directly to::

    from dfe_engine.settings import get_settings
    settings = get_settings()
    ch = settings.clickhouse   # .host, .port, .username, .password, .secure, .verify
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from hyperi_pylib.logger import logger

from dfe_engine.settings import get_settings
from dfe_engine.yaml_utils import yaml_load, yaml_load_string


class ConfigurationError(Exception):
    """Configuration error."""


class DFEConfigLoader:
    """Static utility class for loading DFE configuration."""

    # ------------------------------------------------------------------
    # Target file resolution
    # ------------------------------------------------------------------

    @staticmethod
    def get_config_dir() -> Path:
        config_path = Path.home() / ".dfe"
        if not config_path.is_dir():
            raise ConfigurationError(
                f"DFE configuration directory not found at {config_path}"
            )
        return config_path

    @staticmethod
    def get_target_config_file() -> Path:
        return DFEConfigLoader.get_config_dir() / "dfe_targets.yaml"

    @staticmethod
    def read_target_config_file(targets_file_path: str | None = None) -> Path:
        if targets_file_path is None:
            default_path = DFEConfigLoader.get_config_dir() / "dfe_targets.yaml"
            if not default_path.is_file():
                raise ConfigurationError(
                    f"DFE targets file not found at {default_path}"
                )
            return default_path
        expanded = Path(os.path.expanduser(targets_file_path))
        if not expanded.is_file():
            raise ConfigurationError(
                f"DFE targets file not found at {expanded}"
            )
        return expanded

    # ------------------------------------------------------------------
    # Target config reading (settings cascade)
    # ------------------------------------------------------------------

    @staticmethod
    def read_target_config(
        target_name: str | None = None,
        targets_file_path: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        try:
            config_file = DFEConfigLoader.read_target_config_file(targets_file_path)
            config_data = yaml_load(config_file) or {}
        except (ConfigurationError, FileNotFoundError):
            # Fall back to settings-only config
            return {
                "ch_host": settings.clickhouse.host,
                "ch_port": settings.clickhouse.port,
                "ch_username": settings.clickhouse.username,
                "ch_password": settings.clickhouse.password,
                "ch_secure": settings.clickhouse.secure,
                "ch_verify": settings.clickhouse.verify,
                "target_name": target_name or "settings",
            }

        target_name = target_name or config_data.get("default_target")
        targets = config_data.get("targets", {})

        if target_name and target_name in targets:
            target_config = dict(targets[target_name])
        elif targets:
            # Use first available target
            first_name = next(iter(targets))
            target_config = dict(targets[first_name])
            target_name = first_name
        else:
            target_config = {}

        # Settings cascade — env overrides file
        if settings.clickhouse.host != "localhost":
            target_config["ch_host"] = settings.clickhouse.host
        if settings.clickhouse.port != 9000:
            target_config["ch_port"] = settings.clickhouse.port
        if settings.clickhouse.username != "default":
            target_config["ch_username"] = settings.clickhouse.username
        if settings.clickhouse.password:
            target_config["ch_password"] = settings.clickhouse.password

        target_config["target_name"] = target_name
        return target_config

    @staticmethod
    def read_clickhouse_config(
        target_name: str | None = None,
        targets_file_path: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()

        has_env = (
            settings.clickhouse.host != "localhost"
            or settings.clickhouse.port != 9000
            or settings.clickhouse.username != "default"
            or settings.clickhouse.password != ""
        )

        if has_env:
            return {
                "ch_host": settings.clickhouse.host,
                "ch_port": settings.clickhouse.port,
                "ch_username": settings.clickhouse.username,
                "ch_password": settings.clickhouse.password,
                "ch_secure": settings.clickhouse.secure,
                "ch_verify": settings.clickhouse.verify,
            }

        return DFEConfigLoader.read_target_config(target_name, targets_file_path)

    # ------------------------------------------------------------------
    # Printing / logging
    # ------------------------------------------------------------------

    @staticmethod
    def print_target(
        targets_file_path: str | None = None,
        target_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            cfg = DFEConfigLoader.read_target_config(target_name, targets_file_path)
            logger.info(
                f"Target [{cfg.get('target_name', '?')}]: "
                f"host={cfg.get('ch_host', '?')} "
                f"port={cfg.get('ch_port', '?')} "
                f"user={cfg.get('ch_username', '?')}"
            )
        except Exception as e:
            logger.error(f"Failed to print target: {e}")

    @staticmethod
    def print_default_target(
        targets_file_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        DFEConfigLoader.print_target(targets_file_path=targets_file_path)

    # ------------------------------------------------------------------
    # Package loading
    # ------------------------------------------------------------------

    @staticmethod
    def replace_env_variables(text: str, env_vars: dict[str, str]) -> str:
        pattern = re.compile(r"\$\{(\w+)\}")
        return pattern.sub(
            lambda m: env_vars.get(m.group(1), m.group(0)), text
        )

    @staticmethod
    def load_dfe_package(
        config_file_path: str | None = None,
        require_config: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        path = Path(config_file_path) if config_file_path else Path.cwd() / "dfe_package.yaml"

        if not path.exists():
            if require_config:
                raise FileNotFoundError(f"Configuration file [{path}] not found")
            return {}

        env_vars = {**os.environ}
        content = path.read_text()
        content = DFEConfigLoader.replace_env_variables(content, env_vars)
        config = yaml_load_string(content)

        if config is None and require_config:
            raise ValueError(f"Configuration file [{path}] is empty or invalid")

        return config or {}
