__all__ = [
    'ConfigError',
    'ConfigFileExistsError',
    'ConfigFileNotFoundError'
]

class ConfigError(Exception):
    """
    Base class for config related errors.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class ConfigFileExistsError(ConfigError):
    """
    Raised when the config file already exists.
    """
    def __init__(self, config_file_path: str, custom_message: str = ""):
        super().__init__(f"The config file '{config_file_path}' already exists.", custom_message)

class ConfigFileNotFoundError(ConfigError):
    """
    Raised when the config file is not found.
    """
    def __init__(self, config_file_path: str, custom_message: str = ""):
        super().__init__(f"The config file '{config_file_path}' could not be found.", custom_message)

class ConfigMissingKeyError(ConfigError):
    """
    Raised when a key is not found in a config file.
    """
    def __init__(self, config_file_path: str, key: str, custom_message: str = ""):
        super().__init__(f"The key '{key}' could not be found in the config file '{config_file_path}'.")
