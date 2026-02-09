import numpy as np
import pandas as pd


__all__ = [
    'SchemaError',
    'SchemaFileNotFoundError',
    'SchemaBuilderError',
    'SchemaBuilderCommonHeaderError',
    'SchemaBuilderDirNotFoundError',
    'SchemaBuilderDuplicateSchemaNameError',
    'SchemaBuilderInvalidVersionError',
    'SchemaBuilderMetaSchemaError',
    'SchemaBuilderMissingRequiredFieldsError',
    'SchemaBuilderNoSchemasToBuildError',
    'SchemaBuilderTypeMapsError',
    'SchemaWarning',
    'SchemaBuilderWarning',
    'SchemaBuilderJSONWarning'
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

class SchemaBuilderCommonHeaderError(SchemaBuilderError):
    """
    Raised when an issue occurs when reading a common header file.
    """
    def __init__(self, version: str = "", errors: list[Exception] = None, custom_message: str = ""):
        self.errors = errors
        super().__init__(f"An issue occured whilst reading common header version '{version}'.", custom_message)

class SchemaBuilderDirNotFoundError(SchemaBuilderError):
    """
    Raised when a directory required cannot be found.
    """
    def __init__(self, directory: str, custom_message: str = ""):
        super().__init__(f"The directory '{directory}' could not be found.", custom_message)

class SchemaBuilderDuplicateSchemaNameError(SchemaBuilderError):
    """
    Raised when a schema build conifg has a name defined multiple times.
    """
    def __init__(self, schema_name: str, custom_message: str = ""):
        super().__init__(f"The schema '{schema_name}' is already defined.", custom_message)

class SchemaBuilderInvalidVersionError(SchemaBuilderError):
    """
    Raised when a non-semantic version is identified.
    """
    def __init__(self, version: str, custom_message: str = ""):
        super().__init__(f"Invalid version '{version}'. Please ensure you are using semantic versioning (e.g. v001_000_001).", custom_message)

class SchemaBuilderMetaSchemaError(SchemaBuilderError):
    """
    Raised when an issue occurs when reading a meta schema file.
    """
    def __init__(self, meta_schema_path: str = "", errors: list[Exception] = None, custom_message: str = ""):
        self.errors = errors
        super().__init__(f"An issue occured whilst reading the meta schema at '{meta_schema_path}'.", custom_message)

class SchemaBuilderMissingRequiredFieldsError(SchemaBuilderError):
    """
    Raised when an issue occurs when a required schema field is missing.
    """
    def __init__(self, empty_values: pd.DataFrame, field: str, schema_path: str, field_pk: str = None, custom_message: str = ""):
        if (field["name"] == field_pk):
            multiple_errors = len(empty_values) > 1
            message = f"Empty '{field["name"]}' entr{"ies" if multiple_errors else "y"} identified"
        else:
            multiple_errors = empty_values[field_pk].notna().sum() > 1
            message = f"{field_pk.capitalize()} name{f"s '[{", ".join(str(column) for column in empty_values[field_pk].tolist() if column is not np.nan)}]' are" if multiple_errors else f" '{empty_values[field_pk].tolist()[0]}' is"} missing an entry for '{field["name"]}'"
        super().__init__(f"{message} in '{schema_path}'.", custom_message)

class SchemaBuilderNoSchemasToBuildError(SchemaBuilderError):
    """
    Raised when there are no schemas identified to build.
    """
    def __init__(self, schema_filter: str, derived_schema_filter: str, custom_message: str = ""):
        error_str = ""
        if (schema_filter):
            split_schema_filter = schema_filter.split(",")
            schema_filter_str = f"schema filters '[{", ".join(split_schema_filter)}]'"
            error_str = f" matching {schema_filter_str}"
        if (derived_schema_filter):
            split_derived_schema_filter = derived_schema_filter.split(",")
            derived_schema_filter_str = f"derived schema filters '[{", ".join(split_derived_schema_filter)}]'"
            error_str = f"{f"{error_str} and" if (error_str) else " matching"} {derived_schema_filter_str}"
        
        super().__init__(f"No schemas found{error_str}.", custom_message)

class SchemaBuilderTypeMapsError(SchemaBuilderError):
    """
    Raised when an issue occurs when reading a type maps file.
    """
    def __init__(self, version: str = "", errors: list[Exception] = None, custom_message: str = ""):
        self.errors = errors
        super().__init__(f"An issue occured whilst reading type maps version '{version}'.", custom_message)

class SchemaWarning(Exception):
    """
    Base class for schema related warnings.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(f"{message} {custom_message}".strip() if custom_message else message)

class SchemaBuilderWarning(SchemaWarning):
    """
    Base class for schema builder related warnings.
    """
    def __init__(self, message: str, custom_message: str = ""):
        super().__init__(message, custom_message)

class SchemaBuilderJSONWarning(SchemaBuilderWarning):
    """
    Raised when the JSON type is in use but has been specified not to be.
    """
    def __init__(self, custom_message: str = ""):        
        super().__init__("A JSON mapping has been identified.", custom_message)