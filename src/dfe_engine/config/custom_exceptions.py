__all__ = [
    'ConfigError',
    'ConfigFileExistsError',
    'ConfigFileNotFoundError',
    'ConfigKeyNotFoundError',
    'ConfigNotInitializedError',
    'ConfigWarning',
    'ConfigNoUpdateWarning'
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

class ConfigKeyNotFoundError(ConfigError):
    """
    Raised when a key is not found in a config file.
    """
    def __init__(self, config_file_path: str, config_key: str, custom_message: str = ""):
        super().__init__(f"The key '{config_key}' could not be found in the config file '{config_file_path}'.", custom_message)

class ConfigNotInitializedError(ConfigError):
    """
    Raised when a config key has not been initialized prior to operations.
    """
    def __init__(self, config_file_path: str, root_config_key: str, custom_message: str = ""):
        super().__init__(f"The root key '{root_config_key}' has not been initialised in file '{config_file_path}'.")

class ConfigWarning(Exception):
    """
    Base class for config related warnings.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class ConfigNoUpdateWarning(ConfigWarning):
    """
    Raised when a target already has the specified key value pair.
    """
    def __init__(self, config_file_path: str, custom_message: str = ""):
        super().__init__(f"The config file '{config_file_path}' already matches the provided information.", custom_message)