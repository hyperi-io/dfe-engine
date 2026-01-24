import yaml

from deepdiff import DeepDiff
from hs_pylib import config, logger
from hs_pylib.config import get_settings
from pathlib import Path
from typing import Optional

from .custom_exceptions import ConfigFileNotFoundError, ConfigMissingKeyError
class Config:

    def __init__(
        self,
        config_file_path: Optional[str] = None
    ):
        subdir_name = Path(config_file_path).expanduser().parent.name
        self.config_file_dir = config.init_config_directory(
            config_dir = Path(config_file_path).expanduser().parent.parent,
            create_subdir = True,
            config_subdir_name = subdir_name,
            create_targets = False,
            create_env = False
        ) / subdir_name
        self.config_file_name = Path(config_file_path).expanduser().name
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
        key: str
    ) -> str:
        """
        Provides the specified key path from the config file.
        """
        logger.debug(f"Getting key '{key}' from config file '{self.config_file_path}'...")
        try:
            with open(self.config_file_path, "r") as file:
                config_data = yaml.safe_load(file) or {}
            
            if ("." in key):
                key_value = config_data
                key_path = key.split(".")
                for key_parent in key_path:
                    key_value = key_value[key_parent]
            else:
                key_value = config_data[key]
        
        except FileNotFoundError:
            raise ConfigFileNotFoundError(self.config_file_path)
        
        except KeyError:
            raise ConfigMissingKeyError(self.config_file_path, key)

        return key_value


    def config_update(
        self,
        new_data: dict,
        root_key: str = None
    ) -> dict:
        """
        Updates the config file with the specified new_data in the root_key path (if provided).
        """
        logger.debug(f"Updating config file '{self.config_file_path}' with new data{f" for key '{root_key}" if root_key else ""}...")
        try:
            with open(self.config_file_path, "r") as file:
                old_config_data = yaml.safe_load(file) or {}
            
            if (root_key):
                new_config_data = old_config_data
                root_key_path = root_key.split(".")

                current = new_config_data
                for key in root_key_path[:-1]:
                    if key not in current:
                        current[key] = {}
                    current = current[key]
                
                current[root_key_path[-1]] = new_data
            else:
                new_config_data = new_data
            
            with open(self.config_file_path, "w") as file:
                yaml.dump(new_config_data, file, sort_keys = False)

        except FileNotFoundError:
            raise ConfigFileNotFoundError(self.config_file_path)
            
        return DeepDiff(old_config_data, new_data, ignore_order = True)
