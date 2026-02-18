import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any

import re
from hyperi_pylib.logger import logger

from ..settings import get_settings
from ..yaml_utils import yaml_load, yaml_load_string, YAMLError


class ConfigurationError(Exception):
    """Custom exception for configuration errors."""

    def __init__(self, message):
        super().__init__(message)


class DFEConfigLoader:
    @staticmethod
    def normalize_version(version: str) -> str:
        parts = version.split(".")
        normalized_parts = [part.zfill(3) for part in parts]
        return "".join(normalized_parts)

    @staticmethod
    def get_config_dir() -> Path:
        """Get the directory where configuration files are stored."""
        config_path = Path.home() / ".dfe"

        if not config_path.is_dir():
            logger.error(
                f"DFE configuration directory not found at {config_path}. Please create this directory manually or run 'mkdir -p {config_path}' to create it."
            )
            raise ConfigurationError(
                f"DFE configuration directory not found. Please create the directory at: {config_path}"
            )

        return config_path

    @staticmethod
    def get_target_config_file() -> Path:
        """Get the path to the configuration file."""
        file_path = DFEConfigLoader.get_config_dir() / "dfe_targets.yaml"
        path_obj = Path(file_path)
        if not Path(path_obj).is_file():
            try:
                path_obj.touch(exist_ok=True)
                logger.info(
                    "The dfe_targets.yaml file has been successfully created in the .dfe folder."
                )
            except Exception as error:
                if not hasattr(error, "strerror"):
                    error_msg = ""
                else:
                    error_msg = " " + error.strerror

                logger.error(
                    "ERROR: Unable to create file " + file_path + " " + error_msg,
                    exc_info=True,
                )
                raise ConfigurationError(
                    f"Unable to create dfe_targets.yaml file: {error_msg}"
                ) from error

        return DFEConfigLoader.get_config_dir() / "dfe_targets.yaml"

    @staticmethod
    def read_target_config_file(targets_file_path: Optional[str] = None) -> Path:
        """Get the path to the configuration file."""

        if targets_file_path is None:
            logger.info(
                f"targets config path has not been found, loading the default target file from the home directory. [{targets_file_path}]"
            )
            default_target_path = (
                DFEConfigLoader.get_config_dir() / "dfe_targets.yaml"
            )  # Assume a default or configured directory for DFE config
            if not default_target_path.is_file():
                logger.error(
                    f"Default DFE targets file not found at {default_target_path}. This file should contain your target configurations. Please create it with appropriate target settings.",
                    exc_info=True,
                )
                raise ConfigurationError(
                    f"DFE targets configuration file not found. Please create the file at: {default_target_path} with your target settings."
                )
            return default_target_path
        else:
            logger.info(f"Looking for target config path here [{targets_file_path}]")
            expanded_targets_file_path = os.path.expanduser(targets_file_path)
            path_obj = Path(expanded_targets_file_path)
            logger.info(f"path_obj  [{path_obj}] ")
            if not path_obj.is_file():
                error_msg = f"DFE targets configuration file not found at {path_obj}. Please verify the file path and ensure it contains valid target settings."
                logger.error(error_msg)
                raise ConfigurationError(error_msg)
            return path_obj

    @staticmethod
    def read_target_config(
        target_name: str = None, targets_file_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Read target configuration from file with settings cascade override.

        Settings priority: Environment variables > Config file > defaults.yaml
        """
        settings = get_settings()

        try:
            config_file = DFEConfigLoader.read_target_config_file(
                targets_file_path=targets_file_path
            )
            config_data = yaml_load(config_file) or {}

            target_name = target_name or config_data.get("default_target")

            if target_name not in config_data.get("targets", {}):
                raise ConfigurationError(
                    f"Target '{target_name}' specified but not found in configuration."
                )

            target_config = config_data["targets"][target_name]

            # Apply settings cascade overrides (env vars take precedence via settings module)
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

        except FileNotFoundError as err:
            raise ConfigurationError(f"Configuration file {targets_file_path} not found.") from err
        except YAMLError as err:
            raise ConfigurationError("Failed to parse the configuration file.") from err

    @staticmethod
    def read_clickhouse_config(
        target_name: str = None, targets_file_path: str = None
    ) -> Dict[str, Any]:
        """
        Get ClickHouse configuration using settings cascade.

        Settings priority: Environment variables > Config file > defaults.yaml
        """
        settings = get_settings()

        # Check if env vars were set (non-default values)
        has_env_override = (
            settings.clickhouse.host != "localhost"
            or settings.clickhouse.port != 9000
            or settings.clickhouse.username != "default"
            or settings.clickhouse.password != ""
        )

        if has_env_override:
            logger.info("Using ClickHouse configuration from environment/settings")
            return {
                "ch_host": settings.clickhouse.host,
                "ch_port": settings.clickhouse.port,
                "ch_username": settings.clickhouse.username,
                "ch_password": settings.clickhouse.password,
                "ch_secure": settings.clickhouse.secure,
                "ch_verify": settings.clickhouse.verify,
            }

        # Otherwise read from the targets file
        target_config = DFEConfigLoader.read_target_config(
            target_name=target_name, targets_file_path=targets_file_path
        )
        logger.info("Using ClickHouse configuration from targets file")
        return target_config

    @staticmethod
    def list_targets(targets_file_path: str = None) -> None:
        """List all targets available in the configuration."""
        try:
            config_file = DFEConfigLoader.read_target_config_file(
                targets_file_path=targets_file_path
            )
            if not config_file.exists():
                raise FileNotFoundError(f"Configuration file {config_file} not found.")

            config_data = yaml_load(config_file)
            targets = config_data.get("targets", {}) if config_data else {}
            if targets:
                logger.info("Available targets:")
                for target in targets:
                    logger.info(f"- {target}")
            else:
                logger.info("No targets found in configuration.")
        except FileNotFoundError as err:
            logger.error(f"Configuration file {config_file} not found.")
            raise ConfigurationError(f"Configuration file {config_file} not found.") from err
        except Exception as e:
            logger.error(f"Failed to list targets: {e}")
            raise ConfigurationError(f"Failed to list targets: {e}") from e

    @staticmethod
    def print_default_target(targets_file_path: str = None) -> None:
        """Prints the default target's host, port, and username from the configuration."""
        settings = get_settings()

        try:
            logger.info(f"Getting target from the [{targets_file_path}]")
            default_target_data = DFEConfigLoader.read_target_config(
                targets_file_path=targets_file_path
            )

            if not isinstance(default_target_data, dict):
                raise TypeError(
                    f"read_target_config() returned unexpected type: {type(default_target_data)}"
                )

            # Check if settings override was applied
            has_env_override = (
                settings.clickhouse.host != "localhost" or settings.clickhouse.port != 9000
            )
            config_source = "environment/settings" if has_env_override else "dfe_targets.yaml file"

            target_name = default_target_data.get("target_name", "No target name specified")
            host = default_target_data.get("ch_host", "No host specified")
            port = default_target_data.get("ch_port", "No port specified")
            username = default_target_data.get("ch_username", "No username specified")

            logger.info(
                f"Default [{target_name}] Target Configuration (from {config_source}):\n\tHost: {host}\n\tPort: {port}\n\tUsername: {username}"
            )

        except ConfigurationError as ce:
            logger.error(
                f"Failed to print default target configuration: {ce} with targets_file_path {targets_file_path}"
            )
        except Exception as e:
            logger.error(f"Unexpected error occurred: {e}")
            raise ConfigurationError(
                "An unexpected error occurred while printing default target configuration."
            )

    @staticmethod
    def print_target(
        targets_file_path: str = None,
        target_name: str = None,
        **kwargs,  # Accept and ignore extra kwargs for backward compatibility
    ) -> None:
        """Prints the specified target's host, port, and username from the configuration."""
        settings = get_settings()

        try:
            logger.info(f"Getting target from the [{targets_file_path}]")
            target_config_data = DFEConfigLoader.read_target_config(
                target_name=target_name, targets_file_path=targets_file_path
            )

            if not isinstance(target_config_data, dict):
                raise TypeError(
                    f"read_target_config() returned unexpected type: {type(target_config_data)}"
                )

            # Check if settings override was applied
            has_env_override = (
                settings.clickhouse.host != "localhost" or settings.clickhouse.port != 9000
            )
            config_source = "environment/settings" if has_env_override else "dfe_targets.yaml file"

            file_target_name = target_config_data.get("target_name", "No target name specified")
            host = target_config_data.get("ch_host", "No host specified")
            port = target_config_data.get("ch_port", "No port specified")
            username = target_config_data.get("ch_username", "No username specified")

            logger.info(
                f"Requested {target_name} Configuration (from {config_source}):\nTarget Name: {file_target_name}\n\tHost: {host}\n\tPort: {port}\n\tUsername: {username}"
            )

        except ConfigurationError as ce:
            logger.error(
                f"Failed to print {target_name} configuration: {ce} with targets_file_path {targets_file_path}"
            )
        except Exception as e:
            logger.error(f"Unexpected error occurred: {e}")
            raise ConfigurationError(
                "An unexpected error occurred while printing target configuration."
            )

    @staticmethod
    def replace_env_variables(text: str, env_vars: Dict[str, str]) -> str:
        """
        Replace environment variable placeholders in the text with actual values from env_vars.

        :param text: The input text containing placeholders in the form of ${VAR_NAME}.
        :param env_vars: A dictionary of environment variables to replace in the text.
        :return: The text with placeholders replaced by corresponding environment variable values.
        """
        pattern = re.compile(r"\$\{(\w+)\}")
        return pattern.sub(lambda match: env_vars.get(match.group(1), match.group(0)), text)

    def get_core_package_config(config_data: dict, section: str, keys: list) -> dict:
        """
        Retrieves multiple folder paths from a nested section in the DFE config.

        Args:
            config_data (dict): The loaded DFE configuration dictionary.
            section (str): The top-level section, e.g., 'vector_files'.
            keys (list): List of keys within the section to retrieve folder paths for.

        Returns:
            dict: A dictionary where each key maps to a list of folder paths.
        """
        section_data = config_data.get("global_settings", {}).get(section, {})
        paths = {key: section_data.get(key, []) for key in keys}
        return paths

    @staticmethod
    def _add_external_directory_as_package(dfe_core_config_path: str):
        """Adds the external dfe_core_config directory to sys.path to make it a loadable package."""
        dfe_core_config_dir = Path(dfe_core_config_path).resolve()
        if dfe_core_config_dir.is_dir():
            init_file = dfe_core_config_dir / "__init__.py"
            if not init_file.exists():
                init_file.touch()
            sys.path.insert(0, str(dfe_core_config_dir.parent))
            logger.info(f"Using core_dfe_config directory as package: {dfe_core_config_dir}")
        else:
            raise FileNotFoundError(
                f"Core DFE config path {dfe_core_config_dir} is not a valid directory"
            )

    @staticmethod
    def load_dfe_package(
        config_file_path: Optional[str] = None,
        require_config: bool = True,
    ) -> Dict[str, Any]:
        """
        Load the configuration file. Raises an error if not found or invalid, except when require_config is False.

        :param config_file_path: Path to the configuration file.
        :param require_config: Flag indicating whether the config is required.
        :return: Parsed configuration dictionary or an empty dictionary if require_config is False and the file is not found.
        """
        config_file_path = (
            Path(config_file_path) if config_file_path else Path.cwd() / "dfe_package.yaml"
        )

        if not config_file_path.exists():
            if require_config:
                raise FileNotFoundError(
                    f"Configuration file [{config_file_path}] not found in the current directory: {config_file_path.parent}"
                )
            else:
                logger.warning(f"Configuration file [{config_file_path}] not found.")
                return {}

        try:
            # Environment variables are loaded via settings module
            env_vars = {**os.environ}
            config_content = config_file_path.read_text()
            config_content = DFEConfigLoader.replace_env_variables(config_content, env_vars)
            config = yaml_load_string(config_content)
        except YAMLError as e:
            error_msg = f"Failed to parse [{config_file_path}]. Ensure it is correctly formatted. Error: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

        if config is None and require_config:
            raise ValueError(f"The configuration file [{config_file_path}] is empty or invalid.")

        return config or {}
