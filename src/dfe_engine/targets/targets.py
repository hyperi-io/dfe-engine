import copy
import yaml

from deepdiff import DeepDiff
from hs_pylib import config, logger
from pathlib import Path
from typing import Optional

from .custom_exceptions import TargetNotFoundError, TargetsFileNotFoundError

class Targets:

    def __init__(
        self,
        targets_file_path: Optional[str] = None
    ):
        self.targets_file_dir = config.init_config_directory(
            config_dir = Path(targets_file_path).expanduser().parent,
            create_subdir = False,
            create_targets = False,
            create_env = False
        )
        self.targets_file_name = Path(targets_file_path).expanduser().name
        self.targets_file_path = self.targets_file_dir / self.targets_file_name

    
    def targets_file_exists(
        self
    ) -> bool:
        """
        Checks if the targets file exists.
        """
        logger.debug(f"Checking existence of targets file '{self.targets_file_path}'...")
        return self.targets_file_path.exists()
    

    def targets_file_create(
        self
    ) -> Path:
        """
        Creates the targets file.
        """
        logger.debug(f"Creating targets file '{self.targets_file_path}'...")
        initial_file_data = {
            "default_target": "",
            "targets": {}
        }
        with open(self.targets_file_path, "w") as file:
            yaml.dump(initial_file_data, file)
        
        return self.targets_file_path
    

    def targets_file_list(
        self
    ) -> list[str]:
        """
        Provides a list of the targets present in the targets file.
        """
        logger.debug(f"Listing targets in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        targets = targets_data["targets"]
        list_targets = []
        for target in targets:
            list_targets.append(target)
        
        return list_targets
    

    def targets_file_default_target(
        self
    ) -> str:
        """
        Provides the default_target set in the targets file.
        """
        logger.debug(f"Finding default target in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        return targets_data["default_target"]


    def target_exists(
        self,
        target_name: str
    ) -> bool:
        """
        Finds if the specified target_name exists in the targets file.
        """
        logger.debug(f"Finding existence of '{target_name}' in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        return (target_name in targets_data["targets"])


    def target_has_key(
        self,
        target_name: str,
        target_key: str
    ) -> bool:
        """
        Finds if the specified target_key exists for the specified target_name in the targets file.
        """
        logger.debug(f"Finding if '{target_name}' contains key '{target_key}' in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
            
            targets_data = targets_data["targets"][target_name]
            
            if ("." in target_key):
                target_key_path = target_key.split(".")
                for target_key in target_key_path[:-1]:
                    targets_data = targets_data[target_key]
                target_key = target_key_path[-1]
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        except KeyError:
            return False
        
        return (target_key in targets_data)
    

    def target_is_default(
        self,
        target_name: str
    ) -> bool:
        """
        Finds if the specified target_name is set as the default target in the targets file.
        """
        logger.debug(f"Finding if '{target_name}' is the default target in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        return (target_name == targets_data["default_target"])
    

    def target_get(
        self,
        target_name: str,
        target_key: str = None
    ) -> str | dict | None:
        """
        Provides the specified target_key for target target_name from the targets file or full target if none given.
        """
        logger.debug(f"Getting {f"key '{target_key}' for " if target_key else ""}target '{target_name}' in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
            
            target_data = targets_data["targets"][target_name]
            
            if not(target_key):
                return target_data

            if ("." in target_key):
                target_key_value = target_data
                target_key_path = target_key.split(".")
                for key_parent in target_key_path:
                    if (key_parent not in target_key_value or not(isinstance(target_key_value, dict))):
                        return None
                    target_key_value = target_key_value[key_parent]
            else:
                target_key_value = target_data[target_key]
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        except KeyError:
            return None
        
        return target_key_value


    def target_set_default(
        self,
        target_name: str
    ) -> Path:
        """
        Sets the specified target_name as the default target in the targets file.
        """
        logger.debug(f"Setting '{target_name}' as the default target in '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
            targets_data["default_target"] = target_name

            with open(self.targets_file_path, "w") as file:
                yaml.dump(targets_data, file)
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        return self.targets_file_path
    

    def target_add(
        self,
        target_name: str,
        target_data: dict,
        set_to_default: bool = False
    ) -> Path:
        """
        Adds the specified target_name to the targets file with the specified target_data.
        """
        logger.debug(f"Adding target '{target_name}' to '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
            targets_data["targets"][target_name] = target_data
            
            with open(self.targets_file_path, "w") as file:
                yaml.dump(targets_data, file)

            if (set_to_default or targets_data["default_target"] == ""):
                self.target_set_default(target_name)
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
            
        return self.targets_file_path
    
    
    def target_update(
        self,
        target_name: str,
        new_data: dict,
        set_to_default: bool = False
    ) -> dict:
        """
        Updates the specified target_name with the specified new_data in the targets file.
        """
        logger.debug(f"Updating target '{target_name}' in targets file '{self.targets_file_path}' with new data...")
        try:
            with open(self.targets_file_path, "r") as file:
                old_targets_data = yaml.safe_load(file) or {}
            
            new_targets_data = copy.deepcopy(old_targets_data)
            new_targets_data["targets"][target_name] = new_data

            with open(self.targets_file_path, "w") as file:
                yaml.dump(new_targets_data, file, sort_keys = False)
            
            if (set_to_default or new_targets_data["default_target"] == ""):
                self.target_set_default(target_name)

        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
        
        return DeepDiff(old_targets_data, new_targets_data, ignore_order = True)


    # def target_update(
    #     self,
    #     target_name: str,
    #     target_key_to_update: str,
    #     target_value_to_update: str,
    #     set_to_default: bool = False
    # ) -> Path:
    #     """
    #     Updates the specified target_key_to_update for the target_name in the targets file with the specified target_value_to_update.
    #     """
    #     logger.debug(f"Updating key '{target_key_to_update}' for target '{target_name}' in '{self.targets_file_path}'...")
    #     try:
    #         with open(self.targets_file_path, "r") as file:
    #             targets_data = yaml.safe_load(file) or {}
    #         targets_data["targets"][target_name][target_key_to_update] = target_value_to_update
        
    #         with open(self.targets_file_path, "w") as file:
    #             yaml.dump(targets_data, file)

    #         if (set_to_default):
    #             self.target_set_default(target_name)
        
    #     except FileNotFoundError:
    #         raise TargetsFileNotFoundError(self.targets_file_path)
            
    #     return self.targets_file_path
    
    
    def target_delete(
        self,
        target_name: str
    ) -> Path:
        """
        Deletes the specified target_name from the targets file.
        """
        logger.debug(f"Deleting target '{target_name}' from '{self.targets_file_path}'...")
        try:
            with open(self.targets_file_path, "r") as file:
                targets_data = yaml.safe_load(file) or {}
            del targets_data["targets"][target_name]
            
            with open(self.targets_file_path, "w") as file:
                yaml.dump(targets_data, file)
        
        except FileNotFoundError:
            raise TargetsFileNotFoundError(self.targets_file_path)
            
        return self.targets_file_path


    def get_active_target(
        self,
        target_name: str = None
    ) -> dict:
        """
        Provides the active target data based on hierarchy.
        """
        logger.debug(f"Identifying target to use from '{self.targets_file_path}'...")
        try:
            target_data = config.get_target_config(
                target = target_name,
                targets_file = self.targets_file_path
            )
            logger.debug(f"Using target '{target_data["target_name"]}'.")

            for key, value in target_data.items():
                logger.debug(f"{key}: {value}")
        
        except ValueError:
            raise TargetNotFoundError(self.targets_file_path, target_name)

        return target_data