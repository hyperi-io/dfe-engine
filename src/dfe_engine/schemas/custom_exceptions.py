__all__ = [
    'SchemaError',
    'NoSchemasToBuildError',
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

class NoSchemasToBuildError(SchemaError):
    """
    Raised when there are no schemas identified to build.
    """
    def __init__(self, schema_filter: str, derived_schema_filter: str, custom_message: str = ""):
        error_str = ""
        if (schema_filter):
            schema_filter_str = f"schema filter '[{schema_filter.replace(",", ", ")}]'"
            error_str = f"matching {schema_filter_str}"
        if (derived_schema_filter):
            derived_schema_filter_str = f"derived schema filter '[{derived_schema_filter.replace(",", ", ")}]'"
            error_str = f"{f"{error_str} and" if (error_str) else "matching"} {derived_schema_filter_str}"
        
        super().__init__(f"No schemas found{error_str}.", custom_message)

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