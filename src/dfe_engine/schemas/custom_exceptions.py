__all__ = [
    'SchemaError',
    'SchemaFileNotFoundError',
    'SchemaBuilderError',
    'SchemaBuilderDirNotFoundError'
]

class SchemaError(Exception):
    """
    Base class for schema related errors.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class SchemaFileNotFoundError(SchemaError):
    """
    Raised when a schema file cannot be found.
    """
    def __init__(self, file_path: str, custom_message: str = ""):
        super().__init__(f"The file '{file_path}' could not be found.", custom_message)

class SchemaBuilderError(SchemaError):
    """
    Base class for schema builder related errors.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(message, custom_message)

class SchemaBuilderDirNotFoundError(SchemaBuilderError):
    """
    Raised when a directory required cannot be found.
    """
    def __init__(self, directory: str, custom_message: str = ""):
        super().__init__(f"The directory '{directory}' could not be found.", custom_message)