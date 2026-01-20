__all__ = [
    'ConfigError',
    'ConfigFileNotFoundError'
]

class ConfigError(Exception):
    """
    Base class for config related errors.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class ConfigFileNotFoundError(ConfigError):
    """
    Raised when the config file is not found.
    """
    def __init__(self, config_file_path: str, custom_message: str = ""):
        super().__init__(f"The config file '{config_file_path} could not found.", custom_message)
