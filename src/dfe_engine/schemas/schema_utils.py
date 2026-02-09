import pandas as pd
import re

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderError, SchemaBuilderCommonHeaderError, SchemaBuilderDirNotFoundError, SchemaBuilderInvalidVersionError, SchemaBuilderMetaSchemaError, SchemaBuilderMissingRequiredFieldsError, SchemaBuilderTypeMapsError, SchemaBuilderWarning, SchemaBuilderJSONWarning
from dfe_engine.schemas.schema_field_definitions.common_header_fields import COMMON_HEADER_FIELDS
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
        meta_schema_path: Path = None,
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
        
        self.common_header_path = common_header_path / self.common_header_version / self.COMMON_HEADER_FILE_NAME
        self.meta_schema_path = meta_schema_path
        self.type_maps_path = type_maps_path / self.type_maps_version / self.TYPE_MAPS_FILE_NAME
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
    

    @staticmethod
    def _find_missing_field_errors(
        df_to_search: pd.DataFrame,
        field_definitions: list[dict],
        field_pk: str,
        file_path: Path
    ) -> list[Exception]: # pragma: no cover
        missing_field_errors = []

        for field in field_definitions:
            empty_values = SchemaUtils()._get_empty_column_values_dataframe(field["name"], df_to_search)

            if (field["required"] and not(empty_values.empty)):
                missing_field_errors.append(SchemaBuilderMissingRequiredFieldsError(empty_values, field, file_path, field_pk))
        
        return missing_field_errors


    def get_common_header_df(
        self
    ) -> pd.DataFrame:
        """
        Load a common header CSV file from a resource package.
        """
        try:
            field_pk = "column"
            common_header_df = self._csv_to_dataframe(self.common_header_path)

            common_header_df = common_header_df.reindex(columns = [field["name"] for field in COMMON_HEADER_FIELDS]).sort_values(by = field_pk).reset_index(drop = True)

            missing_field_errors = self._find_missing_field_errors(
                df_to_search = common_header_df,
                field_definitions = COMMON_HEADER_FIELDS,
                field_pk = field_pk,
                file_path = self.common_header_path
            )
        
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderCommonHeaderError(self.common_header_version, missing_field_errors)

        except SchemaError as e:
            raise SchemaBuilderCommonHeaderError(self.common_header_version, e)
                
        return common_header_df


    def get_meta_schema_df(
        self
    ) -> pd.DataFrame:
        """
        Load a meta schema CSV file from a resource package.
        """
        try:
            field_pk = "column"
            meta_schema_df = self._csv_to_dataframe(self.meta_schema_path)

            meta_schema_df = meta_schema_df.reindex(columns = [field["name"] for field in META_SCHEMA_FIELDS]).sort_values(by = field_pk).reset_index(drop = True)

            missing_field_errors = self._find_missing_field_errors(
                df_to_search = meta_schema_df,
                field_definitions = META_SCHEMA_FIELDS,
                field_pk = field_pk,
                file_path = self.meta_schema_path
            )
            
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderMetaSchemaError(self.meta_schema_path, missing_field_errors)

        except SchemaError as e:
            raise SchemaBuilderMetaSchemaError(self.meta_schema_path, e)
                
        return meta_schema_df


    def get_type_maps_df(
        self
    ) -> pd.DataFrame:
        """
        Load a type map CSV file from a resource package.
        """
        try:
            field_pk = "type"
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
                field_pk = field_pk,
                file_path = self.type_maps_path
            )
            
            if (len(missing_field_errors) > 0):
                raise SchemaBuilderTypeMapsError(self.type_maps_version, missing_field_errors)

        except SchemaError as e:
            raise SchemaBuilderTypeMapsError(self.type_maps_version, e)
                
        return type_maps_df