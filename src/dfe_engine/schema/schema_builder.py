from .schema_ch import ClickHouseSchema
from .schema_os import OpenSearchTemplate
import importlib.resources as pkg_resources
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

import os
import fnmatch
import sqlparse
from pathlib import Path
from importlib import resources
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from .schema_util import SchemaUtils, SchemaValidationError, SchemaNameConflictError


class SchemaBuilderException(Exception):
    """Custom exception class for SchemaBuilder errors."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class SchemaInfo(BaseModel):
    name: str
    meta_schema_file_path: Optional[Path] = None
    meta_schema_dir_path: Optional[Path] = None
    additional_fields_file_path: Optional[Path] = None
    meta_schema_no_extension: Optional[str] = None
    meta_schema_extension: Optional[str] = None
    meta_schema_resource: Optional[str] = None
    meta_schema_version: Optional[str] = None
    unified_mapping_file_path: Optional[Path] = None
    derived_schema_file_path: Optional[Path] = None
    derived_schema_ttl: int = Field(default=90)


class SchemaBuilder:
    LOG_BEATS_SCHEMAS = set(
        [
            "logs_beats_auditbeat",
            "logs_beats_filebeat",
            "logs_beats_heartbeat",
            "logs_beats_metricbeat",
            "logs_beats_winlogbeat",
            "logs_beats_packetbeat",
        ]
    )
    COMMON_RESOURES_PACKAGE_NAME = "dfecli.resources"

    def __init__(
        self,
        derived_schema_path: Path,
        logger: logging.Logger = None,
        no_cluster_declarations_needed: bool = True,
        use_replicated_merge_tree: bool = True,
        use_json_feature: bool = False,
        use_subsampling_feature: bool = False,
        use_shared_merge_tree: bool = True,
        config: dict = None,
        schema_filter_list: str = None,
        derived_schema_filter_list: str = None,
        schema_filter_wildchar: str = None,
        derived_schema_filter_wildchar: str = None,
        all=False,
        only_beats=False,
        max_workers: int = 4,
        opensearch_flag=False,
    ):
        self.logger = logger
        self.config = config

        self.derived_schema_path = derived_schema_path

        self.no_cluster_declarations_needed = no_cluster_declarations_needed
        self.use_replicated_merge_tree = use_replicated_merge_tree
        self.use_shared_merge_tree = use_shared_merge_tree

        self.schema_filter_list = schema_filter_list
        self.derived_schema_filter_list = derived_schema_filter_list
        self.schema_filter_wildchar = schema_filter_wildchar
        self.derived_schema_filter_wildchar = derived_schema_filter_wildchar
        self.all = all
        self.only_beats = only_beats

        self.max_workers = max_workers

        self.opensearch_flag = opensearch_flag

        self.organisations = config.get("organisations", [])

        self.common_schema_version = config["global_settings"][
            "schema_common_version"
        ].replace(".", "_")
        self.common_resource_path = f"common/{self.common_schema_version}"

        self.dfe_output_path = Path(config["global_settings"]["schema_output_path"])
        self.schema_common_version = SchemaUtils.normalize_dot_version(
            self.config["global_settings"]["schema_common_version"]
        )
        self.use_json_feature = use_json_feature
        self.use_subsampling_feature = use_subsampling_feature

    @staticmethod
    def validate_norm_column_names(schema_columns: List[Dict[str, Any]]) -> None:
        """
        Validate that indexed ip_field columns don't conflict with user-defined _norm columns.
        
        Raises:
            SchemaNameConflictError if conflict detected
        """
        existing_cols = {col.get('column') for col in schema_columns if col.get('column')}
        
        for col in schema_columns:
            col_name = col.get('column')
            col_type = col.get('type')
            col_index_type = col.get('index_type')
            
            if col_type == 'ip_field' and col_index_type:
                expected_norm_name = f"{col_name}_norm"
                
                if expected_norm_name in existing_cols:
                    error_msg = (
                        f"Conflict: '{expected_norm_name}' already exists. "
                        f"The ip_field '{col_name}' with index_type '{col_index_type}' "
                        f"requires auto-generating a MATERIALIZED column '{expected_norm_name}'. "
                        f"Please rename the manual column or remove the index_type."
                    )
                    raise SchemaNameConflictError(error_msg)

    def __dos2unix(self, output_path):
        for dir_path, _, filenames in os.walk(output_path):
            for filename in filenames:
                file_path = os.path.join(dir_path, filename)
                self.__convert_line_endings(file_path)

    def __convert_line_endings(self, file_path):
        """Convert DOS line endings to Unix line endings in a file."""
        try:
            with open(file_path, "rb") as file:
                content = file.read()
            updated_content = content.replace(b"\r\n", b"\n")
            with open(file_path, "wb") as file:
                file.write(updated_content)
        except IOError as e:
            self.logger.error(f"Error processing file {file_path}: {e.strerror}")
            exit(1)

    def _check_directory_exists(self, directory: Path):
        """Ensure the given directory exists, creating it if necessary."""
        if directory is None:
            self.logger.error(
                "Provided directory path is None. Please provide a valid directory path.",
                stack_info=True,
            )
            return

        if not directory.is_dir():
            directory.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Directory '{directory}' created.")
        else:
            self.logger.info(f"Directory '{directory}' already exists.")

    def _check_file_exists(self, file_path: Path):
        if not file_path.is_file():
            raise FileNotFoundError(f"File '{file_path}' does not exist.")

    def _extract_schema_info(self, schema_name: str, schema_info: dict) -> SchemaInfo:
        if not self.derived_schema_path:
            raise ValueError("derived_schema_path must be set")

        meta_schema_paths = self.config.get("global_settings", {}).get(
            "meta_schema_paths", None
        )
        meta_schema_file_path_base = Path(meta_schema_paths)

        meta_schema = schema_info.get("meta_schema")
        meta_schema_no_extension = meta_schema.split(".")[0] if meta_schema else None
        meta_schema_extension = meta_schema.split(".")[1] if meta_schema else None
        meta_schema_version = (
            schema_info.get("meta_schema_version").replace(".", "_")
            if schema_info.get("meta_schema_version")
            else None
        )
        meta_schema_resource = (
            schema_info.get("meta_schema") if schema_info.get("meta_schema") else None
        )

        unified_mapping_file_path = schema_info.get("unified_schema_mapping", None)
        additional_fields_file_path = schema_info.get("additional_fields_config", None)
        derived_schema_file_path = schema_info.get("derived_schema_file_path", None)

        schema_paths = SchemaInfo(
            name=schema_name,
            meta_schema_no_extension=meta_schema_no_extension,
            meta_schema_extension=meta_schema_extension,
            meta_schema_resource=meta_schema,
            meta_schema_version=meta_schema_version,
            meta_schema_file_path=meta_schema_file_path_base
            / f"{meta_schema_no_extension}/{meta_schema_version}/{meta_schema_resource}",
            meta_schema_dir_path=meta_schema_file_path_base
            / f"{meta_schema_no_extension}/{meta_schema_version}",
            unified_mapping_file_path=self.derived_schema_path
            / unified_mapping_file_path
            if unified_mapping_file_path
            else None,
            additional_fields_file_path=self.derived_schema_path
            / additional_fields_file_path
            if additional_fields_file_path
            else None,
            derived_schema_file_path=self.derived_schema_path / derived_schema_file_path
            if derived_schema_file_path
            else None,
            derived_schema_ttl=schema_info.get("derived_schema_ttl", 90),
        )
        return schema_paths

    def _write_opensearch_framework(self) -> None:
        if not self.opensearch_flag:
            self.logger.warning(
                "OpenSearch framework is not enabled. Operation will be skipped."
            )
            return

        opensearch_dest = self.dfe_output_path / "opensearch" / "HyperSec_Framework"
        self.logger.debug("Copying common OpenSearch Framework")

        # Initialize resource_path to avoid undefined variable error
        resource_path = None
        
        try:
            # Iterate over all JSON files in the resource path
            resource_path = (
                resources.files(SchemaBuilder.COMMON_RESOURES_PACKAGE_NAME)
                / self.common_resource_path
            )
            for resource_rel_path in resource_path.glob("*.json"):
                dest_path = opensearch_dest

                dest_path.mkdir(parents=True, exist_ok=True)
                dest_file_path = dest_path / resource_rel_path.name

                with resource_rel_path.open("r") as f:
                    os_template_str = f.read()

                parsed_json = json.loads(os_template_str)
                with open(dest_file_path, "w") as f:
                    json.dump(parsed_json, f, indent=4)

                self.logger.debug(
                    f"Copied {self.common_resource_path}/{resource_rel_path.name} to {dest_file_path}"
                )

        except Exception as error:
            error_msg = getattr(error, "strerror", str(error))
            path_info = resource_path if resource_path is not None else f"common/{self.common_resource_path}"
            self.logger.error(
                f"Unable to copy OpenSearch templates in package with path [{path_info}] error message {error_msg}"
            )
            exit(1)
        # Usage example
        # Assuming `self.common_path` and `self.dfe_output_path` are defined as Path objects and `self.logger` is defined.
        self.__dos2unix(self.dfe_output_path)
        self.logger.debug("Generating done")

    def _validate_package_resource(
        self, package: str, resource_path: str, schema_name: str, version: str
    ):
        try:
            resource = pkg_resources.files(package) / resource_path
            if not resource.exists():
                error_msg = (
                    f"Schema '{schema_name}' is missing from package '{package}'. "
                    f"Please make sure the schema name is correct and has the correct version number {version}. "
                    f"Also validate the full path of the schema resource '{resource_path}'."
                )
                raise SchemaBuilderException(error_msg)
        except FileNotFoundError:
            error_msg = (
                f"Schema '{schema_name}' is missing from package '{package}'. "
                f"Please make sure the schema name is correct and has the correct version number {version}. "
                f"Also validate the full path of the schema resource '{resource_path}'."
            )
            raise SchemaBuilderException(error_msg)

    def _validate_path(
        self, path: Optional[Path], path_description: str, schema_name: str
    ):
        if path is not None and not path.exists():
            error_msg = (
                f"{path_description} Schema '{schema_name}' does not exist. "
                f"Please ensure that the schema name is correct, "
                f"and that the file or directory exists at the specified path: {path}."
            )
            raise SchemaBuilderException(error_msg)

    def _validate_sql_syntax(self, sql_file: Path):
        try:
            with sql_file.open("r") as file:
                sql_content = file.read()

            parsed_sql = sqlparse.parse(sql_content)
            if not parsed_sql:
                raise ValueError(f"Failed to parse SQL file: {sql_file}")

            self.logger.info(
                f"DDL Validation for SQL {sql_file} Completed Successfully."
            )
        except Exception as e:
            error_msg = f"Syntax error in SQL file {sql_file}: {e}"
            raise SchemaBuilderException(error_msg)

    def validate_multiple_sql_files(self, sql_files: List[Path]):
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(self._validate_sql_syntax, sql_file): sql_file
                for sql_file in sql_files
            }
            for future in as_completed(futures):
                sql_file = futures[future]
                try:
                    future.result()
                except Exception as e:
                    self.logger.error(f"Error validating {sql_file}: {e}")

    def check_all_sql_files(self, output_path: Path):
        sql_files = output_path.glob("**/*.sql")
        self.validate_multiple_sql_files(sql_files)

    def _validate_schema_paths(self, schema_paths: SchemaInfo):
        """Validate schema paths."""
        paths = [
            (schema_paths.derived_schema_file_path, "Sub schema"),
            (schema_paths.additional_fields_file_path, "Additional fields"),
            (schema_paths.unified_mapping_file_path, "Unified mapping"),
        ]
        for path, description in paths:
            if path and not path.exists():
                raise SchemaBuilderException(
                    f"{description} does not exist at path: {path}"
                )

    def build(self):
        try:
            self._check_directory_exists(self.derived_schema_path)
            self._check_directory_exists(self.dfe_output_path)
            schemas = self.config["schemas"]

            matching_schemas = self._find_matching_schemas(schemas)

            extracted_schemas = {}
            for schema_name, schema_info in matching_schemas.items():
                try:
                    schema_paths = self._extract_schema_info(schema_name, schema_info)
                    self._validate_schema_paths(schema_paths)
                    extracted_schemas[schema_name] = schema_paths
                except SchemaBuilderException:
                    self.logger.error(
                        f"Validation failed for schema {schema_name}", stack_info=True
                    )

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                ch_futures = [
                    executor.submit(self._process_clickhouse_schema, schema_paths)
                    for schema_paths in extracted_schemas.values()
                ]

                os_futures = []
                if self.opensearch_flag:
                    os_futures = [
                        executor.submit(
                            self._process_opensearch_templates, schema_paths
                        )
                        for schema_paths in extracted_schemas.values()
                    ]

                self._wait_for_futures(ch_futures + os_futures)

            self.check_all_sql_files(self.dfe_output_path)
            self._write_opensearch_framework()

        except SchemaValidationError as e:
            self.logger.error(
                f"Schema validation failed in build process: {e}", stack_info=True
            )
        except Exception as e:
            self.logger.error(f"Error in build process: {str(e)}", stack_info=True)

    def _wait_for_futures(self, futures):
        """Wait for futures to complete."""
        for future in as_completed(futures):
            try:
                future.result()
            except SchemaValidationError as e:
                self.logger.error(
                    f"Critical schema validation error during processing: {e}"
                )
                print(
                    "Build process stopped due to critical schema validation errors. Please fix the errors and try again."
                )
                raise SystemExit(1)  # Re-raise to ensure the error stops the process
            except Exception as e:
                self.logger.error(
                    f"Unexpected error during processing: {str(e)}", stack_info=True
                )
                raise SystemExit(
                    1
                )  # Optionally, you can stop the process for generic errors as well

    def _pretty_print_schema_info(self, schema_info: SchemaInfo):
        """Pretty print all variables in SchemaInfo for debugging purposes."""
        lines = ["*** SchemaInfo Details: ***\n"]
        for field_name, value in schema_info.dict().items():
            lines.append(f"  {field_name}: {value}")
        lines.append("-------------------\n")
        log_message = "\n".join(lines)
        self.logger.info(log_message)

    def _process_clickhouse_schema(self, schema_info: SchemaInfo):
        """Process an individual ClickHouse schema."""
        try:
            schema_gen_ch = ClickHouseSchema(
                name=schema_info.name,
                version="1",  # TODO: Fix and add schema version to dfe_package if necessary
                meta_schema_file_path=schema_info.meta_schema_file_path,
                common_resource_path=self.common_resource_path,
                derived_schema_full_path=schema_info.derived_schema_file_path,
                additional_fields_full_path=schema_info.additional_fields_file_path,
                dfe_output_path=self.dfe_output_path,
                no_cluster_declarations_needed=self.no_cluster_declarations_needed,
                use_replicated_merge_tree=self.use_replicated_merge_tree,
                use_json_feature=self.use_json_feature,
                use_subsampling_feature=self.use_subsampling_feature,
                use_shared_merge_tree=self.use_shared_merge_tree,
                ttl=schema_info.derived_schema_ttl,
                logger=self.logger,
            )
            schema_gen_ch.build_clickhouse_schema()
            del schema_gen_ch
        except SchemaValidationError as e:
            self.logger.error(f"Schema validation failed: {e}")
            SystemExit(1)
        except Exception as e:
            self.logger.error(
                f"Error processing ClickHouse schemas for {schema_info.name}: {e}"
            )
            SystemExit(1)
        finally:
            self.logger.info(f"ClickHouse Schema Built {schema_info.name}")

    def _process_opensearch_templates(self, schema_info: SchemaInfo):
        """Process an individual OpenSearch template."""
        try:
            self._pretty_print_schema_info(schema_info)
            schema_gen_os = OpenSearchTemplate(
                name=schema_info.name,
                version="1",  # TODO: Fix this
                meta_schema_file_path=schema_info.meta_schema_file_path,
                common_resource_path=self.common_resource_path,
                derived_schema_full_path=schema_info.derived_schema_file_path,
                additional_fields_full_path=schema_info.additional_fields_file_path,
                dfe_output_path=os.path.join(self.dfe_output_path, "opensearch"),
                logger=self.logger,
            )
            schema_gen_os.build_opensearch_template()

            self._write_cm_template(
                schema_info,
                Path(self.dfe_output_path)
                / "opensearch"
                / schema_info.meta_schema_no_extension,
            )

            del schema_gen_os
        except Exception as e:
            self.logger.error(
                f"Error processing OpenSearch template for schema {schema_info.name}: {e}",
                stack_info=True,
            )

    # TODO: We need to look at Dereks new frameworks and maybe just pull all json from the resource template folder.
    def _write_cm_template(self, schema_paths: SchemaInfo, output_path: Path):
        """Write compatibility mode template to the specified output path."""

        if not schema_paths.meta_schema_dir_path:
            self.logger.warning(
                f"No meta schema directory path found for {schema_paths.name}"
            )
            return

        if schema_paths.meta_schema_dir_path.is_dir():
            cm_file = next(
                schema_paths.meta_schema_dir_path.glob(f"{schema_paths.name}_cm.json"),
                None,
            )

            if cm_file and cm_file.is_file():
                try:
                    with cm_file.open("r") as f:
                        cm_template_str = f.read()

                    cm_file_path = output_path / cm_file.name
                    with open(cm_file_path, "w") as f:
                        json.dump(json.loads(cm_template_str), f, indent=4)

                    self.logger.info(
                        f"Copied compatibility mode template from {cm_file} to {cm_file_path}"
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Failed to write compatibility mode template for {schema_paths.name}: {e}"
                    )

            else:
                self.logger.warning(
                    f"No compatibility mode JSON file found in {schema_paths.meta_schema_dir_path}"
                )

        else:
            self.logger.warning(
                f"The provided compatibility mode template path '{schema_paths.meta_schema_dir_path}' is not a directory."
            )

    def _find_matching_schemas(self, schemas):
        """
        Finds all schemas that match the provided self.schema_filter_list
        Returns a dictionary of matching schemas.
        """
        matching_schemas = {}
        schema_filter_list = (
            self.schema_filter_list.split(",") if self.schema_filter_list else []
        )
        derived_schema_filter_list = (
            self.derived_schema_filter_list.split(",")
            if self.derived_schema_filter_list
            else []
        )

        # Skip sigma-related fields
        SIGMA_FIELDS = {"alert_field_defaults", "source_field_mappings"}

        for schema_name, schema_info in schemas.items():
            # Skip sigma-related fields
            if schema_name in SIGMA_FIELDS:
                continue
            if self.all or (self.only_beats and "beats" in schema_name):
                matching_schemas[schema_name] = schema_info
                continue

            # Check for exact matches in filters
            if any(
                schema_name == schema
                for schema in schema_filter_list + derived_schema_filter_list
            ):
                matching_schemas[schema_name] = schema_info
                continue

            # Check for wildcard matches
            if self.schema_filter_wildchar and (
                fnmatch.fnmatch(schema_name, self.schema_filter_wildchar)
                if "*" in self.schema_filter_wildchar
                else self.schema_filter_wildchar == schema_name
            ):
                matching_schemas[schema_name] = schema_info
                continue

            derived_schema_file_path = schema_info.get(
                "derived_schema_file_path", ""
            ).split("/")[-1]
            if self.derived_schema_filter_wildchar and (
                fnmatch.fnmatch(
                    derived_schema_file_path, self.derived_schema_filter_wildchar
                )
                if "*" in self.derived_schema_filter_wildchar
                else self.derived_schema_filter_wildchar == derived_schema_file_path
            ):
                matching_schemas[schema_name] = schema_info
                continue

            # If no filters are applied
            if not (
                self.schema_filter_list
                or self.derived_schema_filter_list
                or self.schema_filter_wildchar
                or self.derived_schema_filter_wildchar
            ):
                matching_schemas[schema_name] = schema_info

        return matching_schemas