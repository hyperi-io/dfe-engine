import fnmatch

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderDirNotFoundError, SchemaBuilderDuplicateSchemaNameError, SchemaBuilderNoSchemasToBuildError
from dfe_engine.schemas.schema import Schema
from dfe_engine.schemas.schema_ch_ddl_generator import SchemaCHDDLGenerator
from dfe_engine.config.config import Config
from hs_pylib import logger
from pathlib import Path


class SchemaBuilder:

    CONFIG_ROOT_KEY = "schemas.build"

    def __init__(
        self,
        derived_schemas_path: Path,
        meta_schemas_path: Path,
        output_path: Path,
        schema_config_path: Path,
        derived_schema_filter: str = None,
        schema_filter: str = None,
        schemas_ttl: int = None,
        type_maps_version: str = None
    ):
        schema_config = Config(
            config_file_path = schema_config_path
        )
        
        self.build_schema_config = schema_config.config_get(self.CONFIG_ROOT_KEY)
        self.derived_schema_filter = derived_schema_filter
        self.derived_schemas_path = Path(derived_schemas_path).expanduser()
        self.meta_schemas_path = Path(meta_schemas_path).expanduser()
        self.output_path = Path(output_path).expanduser()
        self.schema_filter = schema_filter
        self.schemas_ttl = schemas_ttl
        self.type_maps_version = type_maps_version


    def _directory_exists(
        self,
        directory_path: Path
    ) -> bool:
        """
        Internal function to check if a directory exists.
        """
        logger.debug(f"Checking existence of directory '{directory_path}'...")
        if not(directory_path.is_dir()):
            raise SchemaBuilderDirNotFoundError(directory_path)
        
        return (directory_path.is_dir())


    def _filter_schemas(
        self
    ) -> list:
        """
        Internal function that identifies schemas to build based on instance parameters.
        """
        logger.debug("Filtering schemas based off filters set...")
        schemas = self.build_schema_config.get("schemas", [])

        if not(self.schema_filter or self.derived_schema_filter):
            logger.debug("No filters found.")
            return schemas
        
        filtered_schemas = []
        
        filter_list = self.schema_filter.split(",") if self.schema_filter else []
        derived_schema_filter_list = self.derived_schema_filter.split(",") if self.derived_schema_filter else []

        for schema in schemas:
            if (any(fnmatch.fnmatch(schema["name"], filter) for filter in filter_list)):
                filtered_schemas.append(schema)
            
            derived_schema_name = Path(schema["derived_schema_directory"]).name
            if (any(fnmatch.fnmatch(derived_schema_name, filter) for filter in derived_schema_filter_list)):
                filtered_schemas.append(schema)

        return filtered_schemas
    

    def build(
        self
    ) -> None:
        """
        Schema DDL builder from CSV to SQL.
        """
        try:
            self._directory_exists(self.derived_schemas_path)
            self._directory_exists(self.output_path)

            schemas_to_build = self._filter_schemas()

            if (len(schemas_to_build) < 1):
                raise SchemaBuilderNoSchemasToBuildError(self.schema_filter, self.derived_schema_filter)

            log_string = f"'{len(schemas_to_build)}' schema{"s" if len(schemas_to_build) > 1 else ""} to build:"
            for schema in schemas_to_build:
                log_string += f"\n- {schema["name"]}"
            logger.debug(log_string)

            schema_objs = []
            for schema in schemas_to_build:
                try:
                    if (any(schema["name"] == schema_obj.name for schema_obj in schema_objs)):
                        raise SchemaBuilderDuplicateSchemaNameError(schema["name"])
                    
                    schema_obj = Schema(
                        name = schema["name"],
                        derived_schema_directory = schema["derived_schema_directory"],
                        derived_schema_version = schema["derived_schema_version"],
                        derived_schemas_path = self.derived_schemas_path,
                        meta_schema_name = schema["meta_schema"],
                        meta_schema_version = schema["meta_schema_version"],
                        meta_schemas_path = self.meta_schemas_path,
                        ttl = schema.get("ttl", self.schemas_ttl)
                    )
                    schema_objs.append(schema_obj)
                
                except SchemaError as e:
                    logger.warning(f"{e} Skipping schema name '{schema["name"]}'.")
                    continue
            
            for schema_obj in schema_objs:
                ch_ddl = SchemaCHDDLGenerator(
                    type_maps_version = self.type_maps_version
                )

        except Exception:
            raise