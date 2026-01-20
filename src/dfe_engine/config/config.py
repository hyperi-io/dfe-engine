from hs_pylib import config, logger
from hs_pylib.config import get_settings
from pathlib import Path
from typing import Optional

class Config:

    def __init__(
        self,
        config_file_dir: Optional[str] = None,
        config_file_name: Optional[str] = "config.yaml"
    ):
        self.config_file_dir = config.init_config_directory(
            config_dir = Path(config_file_dir).expanduser(),
            create_targets = False,
            create_env = False
        )
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