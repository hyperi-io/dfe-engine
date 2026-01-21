import yaml

from hs_pylib import config, logger
from hs_pylib.config import get_settings
from pathlib import Path
from typing import Optional

from .custom_exceptions import ConfigFileNotFoundError
class Config:

    def __init__(
        self,
        config_file_dir: Optional[str] = None,
        config_file_name: Optional[str] = "config.yaml"
    ):
        self.config_file_dir = config.init_config_directory(
            config_dir = Path(config_file_dir).expanduser().parent,
            config_subdir_name = Path(config_file_dir).expanduser().name,
            create_targets = False,
            create_env = False
        ) / Path(config_file_dir).expanduser().name
        self.config_file_name = config_file_name
        self.config_file_path = self.config_file_dir / self.config_file_name

    
    def config_file_exists(
        self
    ) -> bool:
        """
        Checks if the config file exists.
        """
        logger.debug(f"Checking existence of config file '{self.config_file_path}'...")
        return self.config_file_path.exists()

    
    def config_file_create(
        self
    ) -> Path:
        """
        Creates the config file.
        """
        logger.debug(f"Creating config file '{self.config_file_path}'...")
        initial_file_data = {}
        with open(self.config_file_path, "w") as file:
            yaml.dump(initial_file_data, file)
        return self.config_file_path
    

    def config_get_key(
        self,
        key_path: list[str]
    ) -> str:
        """
        Provides the specified key path from the config file.
        """
        logger.debug(f"Getting key '{'.'.join(key_path)}' from config file '{self.config_file_path}'...")
        try:
            with open(self.config_file_path, "r") as file:
                config_data = yaml.safe_load(file) or {}
            
            value = config_data
            for key in key_path:
                value = value[key]
        except FileNotFoundError:
            raise ConfigFileNotFoundError(self.config_file_path)
        except KeyError:
            raise

        return value
