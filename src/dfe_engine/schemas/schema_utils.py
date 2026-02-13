import pandas as pd
import re

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderError, SchemaBuilderCommonHeaderError, SchemaBuilderDerivedSchemaError, SchemaBuilderDirNotFoundError, SchemaBuilderDuplicatePrimaryKeyError, SchemaBuilderInvalidVersionError, SchemaBuilderMetaSchemaError, SchemaBuilderMissingRequiredFieldsError, SchemaBuilderTypeMapsError, SchemaBuilderWarning, SchemaBuilderDuplicatePrimaryKeyWarning, SchemaBuilderJSONWarning
from dfe_engine.schemas.schema_field_definitions.common_header_fields import COMMON_HEADER_FIELDS
from dfe_engine.schemas.schema_field_definitions.derived_schema_fields import DERIVED_SCHEMA_FIELDS
from dfe_engine.schemas.schema_field_definitions.meta_schema_fields import META_SCHEMA_FIELDS
from dfe_engine.schemas.schema_field_definitions.type_maps_fields import TYPE_MAPS_FIELDS
from hs_pylib import logger
from importlib import resources
from pathlib import Path


class SchemaUtils:

    RESOURCES_PACKAGE_PATH = "dfe_engine.dfe-data-resources"

    COMMON_HEADER_FILE_NAME = "common_header.csv"
    COMMON_HEADER_PATH = "data/common_headers"

    TYPE_MAPS_FILE_NAME = "type_maps.csv"
    TYPE_MAPS_PATH = "data/type_maps"

    SEMVER_PATTERN = re.compile(r'^v\d{3}_\d{3}_\d{3}$')

    def __init__(
        self,
        common_header_path: Path = None,
        common_header_version: str = None,
        type_maps_path: Path = None,
        type_maps_version: str = None,
        use_json_feature: bool = False
    ):
        resources_path = resources.files(self.RESOURCES_PACKAGE_PATH)._paths[0]

        self.common_header_version = self._get_version(
            path = resources_path / self.COMMON_HEADER_PATH,
            version = common_header_version
        )

        self.type_maps_version = self._get_version(
            path = resources_path / self.TYPE_MAPS_PATH,
            version = type_maps_version
        )

        if (common_header_path is None):
            common_header_path = resources_path / self.COMMON_HEADER_PATH
        
        if (type_maps_path is None):
            type_maps_path = resources_path / self.TYPE_MAPS_PATH
        
        self.common_header_path = Path(common_header_path) / self.common_header_version / self.COMMON_HEADER_FILE_NAME
        self.type_maps_path = Path(type_maps_path) / self.type_maps_version / self.TYPE_MAPS_FILE_NAME
        self.use_json_feature = use_json_feature


    @staticmethod
    def _csv_to_dataframe(
        csv_path: Path
    ) -> pd.DataFrame: # pragma: no cover
        """
        Internal function that loads a csv into a dataframe.
        """
        try:
            return pd.read_csv(csv_path)
        
        except Exception as e:
            raise SchemaError(e)


    def _extract_duplicates(
        self,
        dataframe: pd.DataFrame,
        on_error_exception: Exception,
        schema_path: Path,
        source: str,
        unique_key: str,
        initial_row_dict: dict = None,
        schema_version: str = None
    ) -> dict: # pragma: no cover
        duplicate_field_exceptions = []

        if (initial_row_dict is None):
            row_dict = {}
        else:
            row_dict = initial_row_dict

        for _, row in dataframe.iterrows():
            key = row[unique_key]
            if (key in row_dict):
                if (row_dict[key]["type"] == row["type"]):
                    duplicate_field_exceptions.append(SchemaBuilderDuplicatePrimaryKeyWarning(key, row_dict[key]["source"], unique_key, schema_path))
                else:
                    duplicate_field_exceptions.append(SchemaBuilderDuplicatePrimaryKeyError(key, row_dict[key]["source"], unique_key, schema_path))
            else:
                row["source"] = source
                row_dict[key] = row
        
        if (len(duplicate_field_exceptions) > 0):
            if (isinstance(on_error_exception, SchemaBuilderCommonHeaderError)):
                raise on_error_exception(schema_version, duplicate_field_exceptions)
            else:
                raise on_error_exception(schema_path, duplicate_field_exceptions)
        
        return row_dict


    def _find_missing_field_errors(
        self,
        df_to_search: pd.DataFrame,
        field_definitions: list[dict],
        file_path: Path
    ) -> list[Exception]: # pragma: no cover
        missing_field_errors = []
        field_pk = next((field["name"] for field in field_definitions if (field.get("is_key"))), None)

        for field in field_definitions:
            empty_values = self._get_empty_column_values_dataframe(field["name"], df_to_search)

            if (field["required"] and not(empty_values.empty)):
                missing_field_errors.append(SchemaBuilderMissingRequiredFieldsError(empty_values, field, file_path, field_pk))
        
        return missing_field_errors


    @staticmethod
    def _get_empty_column_values_dataframe(
        column_name: str,
        dataframe: pd.DataFrame
    ) -> pd.DataFrame: # pragma: no cover
        """
        Internal function that extracts the empty values in the specified column_name from a dataframe.
        """
        return (dataframe[dataframe[column_name].isna() | (dataframe[column_name].astype(str).str.strip() == "")])


    @staticmethod
    def _get_version(
        path: Path = None,
        version: str = None
    ) -> str: # pragma: no cover
        """
        Internal function that normalises specified version or finds the latest version in a directory.
        """
        def is_valid_version(
            version: str
        ) -> bool:
            logger.debug(f"Checking if version '{version}' is a valid semantic version...")

            return bool(SchemaUtils.SEMVER_PATTERN.match(version))
        
        if (version):
            logger.debug(f"Normalizing version '{version}'...")
            version = version.replace(".", "_")
        elif (path):
            try:
                logger.debug(f"Finding latest version in path '{path}'...")
                version = max(dir.name for dir in path.iterdir() if dir.is_dir())
            except FileNotFoundError:
                raise SchemaBuilderDirNotFoundError(path)
        
        if not(is_valid_version(version)):
            raise SchemaBuilderInvalidVersionError(version)
        
        return version
    

    def create_combined_df(
        self,
        common_header_df: pd.DataFrame,
        derived_schema_path: Path,
        meta_schema_path: Path,
        type_maps_df: pd.DataFrame
    ) -> pd.DataFrame:
        try:
            derived_schema_df = self.get_derived_schema_df(
                derived_schema_path = derived_schema_path
            )
            meta_schema_df = self.get_meta_schema_df(
                meta_schema_path = meta_schema_path
            )

            combined_rows = {}

            combined_rows = self._extract_duplicates(
                dataframe = common_header_df,
                on_error_exception = SchemaBuilderCommonHeaderError,
                unique_key = next((field["name"] for field in COMMON_HEADER_FIELDS if (field.get("is_key"))), None),
                schema_path = self.common_header_path,
                source = "common_header",
                initial_row_dict = combined_rows,
                schema_version = self.common_header_version
            )

            derived_schema_add_df = derived_schema_df[derived_schema_df["type"].notna()]
            derived_schema_sub_df = derived_schema_df[derived_schema_df["type"].isna()]

            combined_rows = combined_rows | self._extract_duplicates(
                dataframe = derived_schema_add_df,
                on_error_exception = SchemaBuilderDerivedSchemaError,
                unique_key = next((field["name"] for field in DERIVED_SCHEMA_FIELDS if (field.get("is_key"))), None),
                schema_path = derived_schema_path,
                source = "derived",
                initial_row_dict = combined_rows
            )

            print(combined_rows)

        
        except SchemaBuilderError as e:
            for error in e.errors:
                if isinstance(error, SchemaBuilderDuplicatePrimaryKeyWarning):
                    logger.warning(f"SchemaBuilderDuplicatePrimaryKeyWarning: {error}")
            
            e.errors = [error for error in e.errors if (isinstance(error, SchemaBuilderDuplicatePrimaryKeyError))]
            if (e.errors):
                raise e


    def get_common_header_df(
        self
    ) -> pd.DataFrame:
        """
        Load a common header CSV file from a resource package.
        """
        try:
            field_pk = next((field["name"] for field in COMMON_HEADER_FIELDS if (field.get("is_key"))), None)
            common_header_df = self._csv_to_dataframe(self.common_header_path)

            common_header_df = common_header_df.reindex(columns = [field["name"] for field in COMMON_HEADER_FIELDS]).sort_values(by = field_pk).reset_index(drop = True)

            missing_field_errors = self._find_missing_field_errors(
                df_to_search = common_header_df,
                field_definitions = COMMON_HEADER_FIELDS,
                file_path = self.common_header_path
            )
        
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderCommonHeaderError(self.common_header_version, missing_field_errors)
            
            logger.success(f"Successfully imported common headers version '{self.common_header_version}'.")
            logger.debug(f"Using the following common_header_df...\n{common_header_df}")

        except SchemaBuilderCommonHeaderError:
            raise

        except SchemaError:
            raise SchemaBuilderCommonHeaderError(self.common_header_version)
                
        return common_header_df


    def get_derived_schema_df(
        self,
        derived_schema_path
    ) -> pd.DataFrame:
        """
        Load a derived schema CSV file.
        """
        try:
            field_pk = next((field["name"] for field in DERIVED_SCHEMA_FIELDS if (field.get("is_key"))), None)
            derived_schema_df = self._csv_to_dataframe(derived_schema_path)

            derived_schema_df = derived_schema_df.reindex(columns = [field["name"] for field in DERIVED_SCHEMA_FIELDS]).sort_values(by = field_pk).reset_index(drop = True)

            missing_field_errors = self._find_missing_field_errors(
                df_to_search = derived_schema_df,
                field_definitions = DERIVED_SCHEMA_FIELDS,
                file_path = derived_schema_path
            )
            
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderDerivedSchemaError(derived_schema_path, missing_field_errors)
            
            logger.success(f"Successfully imported derived schema from '{derived_schema_path}'.")

        except SchemaBuilderDerivedSchemaError:
            raise

        except SchemaError:
            raise SchemaBuilderDerivedSchemaError(derived_schema_path)
                
        return derived_schema_df


    def get_meta_schema_df(
        self,
        meta_schema_path
    ) -> pd.DataFrame:
        """
        Load a meta schema CSV file.
        """
        try:
            field_pk = next((field["name"] for field in META_SCHEMA_FIELDS if (field.get("is_key"))), None)
            meta_schema_df = self._csv_to_dataframe(meta_schema_path)

            meta_schema_df = meta_schema_df.reindex(columns = [field["name"] for field in META_SCHEMA_FIELDS]).sort_values(by = field_pk).reset_index(drop = True)

            missing_field_errors = self._find_missing_field_errors(
                df_to_search = meta_schema_df,
                field_definitions = META_SCHEMA_FIELDS,
                file_path = meta_schema_path
            )
            
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderMetaSchemaError(meta_schema_path, missing_field_errors)
            
            logger.success(f"Successfully imported meta schema from '{meta_schema_path}'.")

        except SchemaBuilderMetaSchemaError:
            raise

        except SchemaError:
            raise SchemaBuilderMetaSchemaError(meta_schema_path)
                
        return meta_schema_df


    def get_type_maps_df(
        self
    ) -> pd.DataFrame:
        """
        Load a type map CSV file from a resource package.
        """
        try:
            field_pk = next((field["name"] for field in TYPE_MAPS_FIELDS if (field.get("is_key"))), None)
            type_maps_df = self._csv_to_dataframe(self.type_maps_path)

            type_maps_df = type_maps_df.reindex(columns = [field["name"] for field in TYPE_MAPS_FIELDS]).sort_values(by = field_pk).reset_index(drop = True)

            try:
                if ("json" in type_maps_df["type"].values and not(self.use_json_feature)):
                    raise SchemaBuilderJSONWarning("The 'use_json_feature' is set to False. Continuing using the string mapping for the JSON type...")
            
            except SchemaBuilderWarning:
                if ("string" not in type_maps_df["type"].values):
                    raise SchemaBuilderError(f"The type_maps file '{self.type_maps_path}' is missing a 'string' type definition.")
                
                type_maps_df.loc[type_maps_df["type"] == "json", type_maps_df.columns != "type"] = type_maps_df.loc[type_maps_df["type"] == "string", type_maps_df.columns != "type"].values

            missing_field_errors = self._find_missing_field_errors(
                df_to_search = type_maps_df,
                field_definitions = TYPE_MAPS_FIELDS,
                file_path = self.type_maps_path
            )
            
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderTypeMapsError(self.type_maps_version, missing_field_errors)
            
            logger.success(f"Successfully imported type maps version '{self.type_maps_version}'.")
            logger.debug(f"Using the following type_maps_df...\n{type_maps_df}")

        except SchemaBuilderTypeMapsError:
            raise

        except SchemaError:
            raise SchemaBuilderTypeMapsError(self.type_maps_version)
                
        return type_maps_df