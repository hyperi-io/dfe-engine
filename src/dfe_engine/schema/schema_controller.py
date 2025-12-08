import os
import sys
from pathlib import Path
from importlib import resources
from zipfile import ZipFile

import pandas as pd
from tabulate import tabulate
from typing import Any, List
from hs_lib.logger import logger
from ..config.config_loader import DFEConfigLoader
from ..clickhouse.clickhouse_manager import ClickHouseManager
from .schema_builder import SchemaBuilder
from .schema_executor import SchemaExecutor
from .schema_plan import SchemaPlan
from .schema_update import SchemaModifier
from .schema_util import SchemaUtils
import threading


class SchemaController:
    lock = threading.Lock()

    @staticmethod
    def build_schemas(
        args_dfe_package_file_path: str,
        args_schema_directory: str,
        args_no_cluster_declarations_needed: bool,
        args_use_replicated_merge_tree: bool,
        args_use_json_feature: bool,
        args_use_subsampling_feature: bool,
        args_use_shared_merge_tree: bool,
        args_log_path: str,
        args_target: str,
        args_target_file_path: str,
        args_all: bool = False,
        args_only_beats: bool = False,
        args_schema_filter_list: str = None,
        args_derived_schema_filter_list: str = None,
        args_schema_filter_wildchar: str = None,
        args_derived_schema_filter_wildchar: str = None,
        verbose: bool = False,
        args_max_workers: int = 4,
    ) -> None:
        """
        Build schemas based on the given configuration.

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_schema_directory (str): Path to custom schema directory.
            args_no_cluster_declarations_needed (bool): Remove references to the cluster.
            args_use_replicated_merge_tree (bool): Use Replicated Merge Trees.
            args_use_json_feature(bool): Use JSON Feature.
            args_use_subsampling_feature (bool): Use Sub Sampling Feature.
            args_use_shared_merge_tree (bool): Use Shared Merge Trees.
            args_log_path (str): Path to the logging directory.
            args_target (str): Target name for the specific environment.
            args_target_file_path (str): Path to the target's configuration file.
            args_all (bool): Flag to process all schemas.
            args_only_beats (bool): Flag to scan for all beats schemas.
            args_schema_filter_list (str): A comma seperate list to filter the schemas the action runs on.
            args_derived_schema_filter_list (str): A comma seperate list to filter the sub-schemas the action runs on.
            args_schema_filter_wildchar (str): Filter the schemas using wildchar.
            args_derived_schema_filter_wildchar (str): Filter the sub-schemas using wildchar.
            verbose (bool): For detailed logging.
            args_max_workers (int): Max number of workers for multithreading
        """

        with SchemaController.lock:
            try:
                dfe_config = DFEConfigLoader.load_dfe_package(
                    config_file_path=args_dfe_package_file_path
                )
            except FileNotFoundError as error:
                logger.error(f"Error Loading dfe_package: {error}")
                return

            try:
                DFEConfigLoader.print_target(
                    target_name=args_target,
                    targets_file_path=args_target_file_path,
                    logger=logger,
                )
            except FileNotFoundError as error:
                logger.error(f"Unable to load target: {error}", exc_info=True)
                sys.exit(1)

            schema_directory = (
                args_schema_directory or dfe_config["global_settings"]["derived_schema_paths"]
            )
            derived_schema_path = Path(schema_directory)

            no_cluster_declarations_needed = (
                args_no_cluster_declarations_needed
                if args_no_cluster_declarations_needed is not None
                else dfe_config["build_schemas"].get("no_cluster_declarations_needed", True)
            )
            use_replicated_merge_tree = (
                args_use_replicated_merge_tree
                if args_use_replicated_merge_tree is not None
                else dfe_config["build_schemas"].get("use_replicated_merge_tree", True)
            )
            use_json_feature = args_use_json_feature or dfe_config["global_settings"].get(
                "use_json_feature", False
            )
            use_shared_merge_tree = (
                args_use_shared_merge_tree
                if args_use_shared_merge_tree is not None
                else dfe_config["build_schemas"].get("use_shared_merge_tree", True)
            )
            use_subsampling_feature = args_use_subsampling_feature or dfe_config[
                "global_settings"
            ].get("use_subsampling_feature", False)

            try:
                schema_builder = SchemaBuilder(
                    config=dfe_config,
                    logger=logger,
                    derived_schema_path=derived_schema_path,
                    schema_filter_list=args_schema_filter_list,
                    derived_schema_filter_list=args_derived_schema_filter_list,
                    schema_filter_wildchar=args_schema_filter_wildchar,
                    derived_schema_filter_wildchar=args_derived_schema_filter_wildchar,
                    no_cluster_declarations_needed=no_cluster_declarations_needed,
                    use_replicated_merge_tree=use_replicated_merge_tree,
                    use_json_feature=use_json_feature,
                    use_subsampling_feature=use_subsampling_feature,
                    use_shared_merge_tree=use_shared_merge_tree,
                    all=args_all,
                    only_beats=args_only_beats,
                    max_workers=args_max_workers,
                )
                schema_builder.build()
                logger.debug("Schema building completed successfully and output is here.")

            except Exception as e:
                logger.error(f"An error occurred while schemas planning: {e}", exc_info=True)

    @staticmethod
    def plan_schemas(
        args_dfe_package_file_path: str,
        args_log_path: str,
        args_target: str,
        args_target_file_path: str,
        args_schema_filter_list: str = None,
        args_derived_schema_filter_list: str = None,
        args_schema_filter_wildchar: str = None,
        args_derived_schema_filter_wildchar: str = None,
        args_org_filter_list: str = None,
        verbose: bool = False,
        is_api_call: bool = False,
    ) -> List[dict]:
        """
        Plan Schemas

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_log_path (str): Path to the logging directory.
            args_target (str): Target name for the specific environment.
            args_target_file_path (str): Path to the target's configuration file.
            args_schema_filter_list (str): A comma seperate list to filter the schemas the action runs on planning.
            args_derived_schema_filter_list (str): A comma seperate list to filter the sub-schemas the action runs on planning.
            args_schema_filter_wildchar (str): Filter the schemas for planning.
            args_derived_schema_filter_wildchar (str): Filter the sub-schemas for planning.
            args_org_filter_list (str): A comma seperate list to filter the org the action runs on.
            verbose (bool): For detailed logging.
            is_api_call (bool): Whether this is being called from a FastAPI endpoint.

        Returns:
            List: If is_api_call is True, returns a list of plan results. Otherwise returns None.
        """

        try:
            DFEConfigLoader.print_target(
                target_name=args_target,
                targets_file_path=args_target_file_path,
                logger=logger,
            )
        except FileNotFoundError as error:
            logger.error(f"Unable to load target: {error}", exc_info=True)
            if is_api_call:
                return [{"status": "error", "error_message": f"Unable to load target: {error}"}]
            sys.exit(1)

        try:
            dfe_config = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path
            )
            dfe_config_target_path = dfe_config["global_settings"].get("target_path", None)
        except FileNotFoundError as error:
            logger.error(f"Error: {error}")
            if is_api_call:
                return [{"status": "error", "error_message": f"Error loading DFE package: {error}"}]
            return

        try:
            target_path = args_target_file_path if args_target_file_path else dfe_config_target_path
            target_config_data = DFEConfigLoader.read_target_config(
                target_name=args_target, targets_file_path=target_path
            )

        except FileNotFoundError as error:
            logger.error(f"Unable to load target: {error}", exc_info=True)
            if is_api_call:
                return [{"status": "error", "error_message": f"Unable to load target: {error}"}]
            sys.exit(1)

        dfe_output_path = Path(dfe_config["global_settings"]["schema_output_path"])
        dfe_output_path.mkdir(parents=True, exist_ok=True)
        organisations = dfe_config.get("organisations", [])

        if args_org_filter_list:
            organisation_keys = set(args_org_filter_list.split(","))
            organisations = [org for org in organisations if org.get("org_id") in organisation_keys]
            logger.info(f"Filtered organisations: {organisations}")
        else:
            organisations = dfe_config.get("organisations", [])

        try:
            if not organisations:
                temp_error_message = ""
                if args_org_filter_list:
                    temp_error_message = f"with key '{args_org_filter_list}' "
                err_msg = f"Error: No organisations found. {temp_error_message} This may occur if the configuration file is missing or does not include any organisation entries. Please ensure that your configuration is correctly set up before running this command."
                if is_api_call:
                    return [{"status": "error", "error_message": err_msg}]
                raise Exception(err_msg)

            plan_schemas = SchemaPlan(
                dfe_output_directory=dfe_output_path,
                organisations=organisations,
                target_config_data=target_config_data,
                schema_filter_list=args_schema_filter_list,
                derived_schema_filter_list=args_derived_schema_filter_list,
                schema_filter_wildchar=args_schema_filter_wildchar,
                derived_schema_filter_wildchar=args_derived_schema_filter_wildchar,
                logger=logger,
            )
            results = plan_schemas.process_sql_scripts(is_api_call=is_api_call)
            logger.info("Schema Plan completed successfully.")
            return results

        except Exception as e:
            logger.error(f"An error occurred while schemas planning: {e}", exc_info=True)
            raise

    @staticmethod
    def modify_schemas(
        args_dfe_package_file_path: str,
        args_schema_update_flag: str,
        args_drop_replacement_table_flag: str,
        args_use_replicated_merge_tree: bool,
        args_use_json_feature: bool,
        args_use_subsampling_feature: bool,
        args_use_shared_merge_tree: bool,
        args_do_add_columns: bool,
        args_log_path: str,
        args_target: str,
        args_target_file_path: str,
        args_max_insert_threads: int,
        args_min_insert_block_size_rows: int,
        args_schema_filter_list: str = None,
        args_derived_schema_filter_list: str = None,
        args_schema_filter_wildchar: str = None,
        args_derived_schema_filter_wildchar: str = None,
        args_org_filter_list: str = None,
        verbose: bool = False,
        is_api_call: bool = False,
    ) -> list[dict]:
        """
        Apply schema configurations and execute SQL scripts.

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_log_path (str): Path to the logging directory.
            args_target (str): Target name for the specific environment.
            args_target_file_path (str): Path to the target's configuration file.
            args_max_insert_threads (str): Max Insert Threads for migrating to new tables.
            args_min_insert_block_size_rows (str): Minimum Insert block size for migrating to new tables.
            args_schema_filter_list (str): A comma seperate list to filter the schemas the action runs on update.
            args_derived_schema_filter_list (str): A comma seperate list to filter the sub-schemas the action runs on update.
            args_schema_filter_wildchar (str): Filter the schemas for update.
            args_derived_schema_filter_wildchar (str): Filter the sub-schemas for update.
            args_org_filter_list (str): A key to lookup for which customers to update schemas for.
            args_use_json_feature (bool) : Using JSON Feature
            args_schema_update_flag (str): Schema update flag.
            verbose (bool): For detailed logging.
            is_api_call (bool): Whether this is being called from a FastAPI endpoint.

        Returns:
            list[dict]: If is_api_call is True, returns a list of modification results. Otherwise returns None.
        """

        try:
            dfe_config = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path
            )
            dfe_config_target_path = dfe_config["global_settings"].get("target_path", None)
        except FileNotFoundError as error:
            logger.error(f"Error: {error}")
            if is_api_call:
                return [{"status": "error", "error_message": f"Error loading DFE package: {error}"}]
            return

        try:
            target_path = args_target_file_path if args_target_file_path else dfe_config_target_path
            target_config_data = DFEConfigLoader.read_target_config(
                target_name=args_target, targets_file_path=target_path
            )

        except FileNotFoundError as error:
            logger.error(f"Unable to load target: {error}", exc_info=True)
            if is_api_call:
                return [{"status": "error", "error_message": f"Unable to load target: {error}"}]
            sys.exit(1)

        dfe_output_path = Path(dfe_config["global_settings"]["schema_output_path"])

        if not dfe_output_path.exists():
            logger.error(
                "The '.dfe_schema_output' directory does not exist in the expected location."
            )
            if is_api_call:
                return [
                    {
                        "status": "error",
                        "error_message": "The '.dfe_schema_output' directory does not exist in the expected location.",
                    }
                ]
            return

        use_replicated_merge_tree = (
            args_use_replicated_merge_tree
            if args_use_replicated_merge_tree is not None
            else dfe_config["build_schemas"].get("use_replicated_merge_tree", True)
        )
        use_shared_merge_tree = (
            args_use_shared_merge_tree
            if args_use_shared_merge_tree is not None
            else dfe_config["build_schemas"].get("use_shared_merge_tree", True)
        )
        do_add_columns = args_do_add_columns or dfe_config["apply_schemas"].get(
            "do_add_columns", False
        )
        use_json_feature = args_use_json_feature or dfe_config["global_settings"].get(
            "use_json_feature", False
        )
        use_subsampling_feature = args_use_subsampling_feature or dfe_config["global_settings"].get(
            "use_subsampling_feature", False
        )
        organisations = dfe_config.get("organisations", [])

        if args_org_filter_list:
            organisation_keys = set(args_org_filter_list.split(","))
            organisations = [org for org in organisations if org.get("org_id") in organisation_keys]
            logger.info(f"Filtered organisations: {organisations}")
        else:
            organisations = dfe_config.get("organisations", [])

        try:
            if not organisations:
                temp_error_message = ""
                if args_org_filter_list:
                    temp_error_message = f"with key '{args_org_filter_list}' "
                err_msg = f"Error: No organisations found. {temp_error_message} This may occur if the configuration file is missing or does not include any organisation entries. Please ensure that your configuration is correctly set up before running this command."
                if is_api_call:
                    return [{"status": "error", "error_message": err_msg}]
                raise Exception(err_msg)

            schema_executor = SchemaModifier(
                dfe_output_directory=dfe_output_path,
                schema_update_flag=args_schema_update_flag,
                drop_replacement_table_flag=args_drop_replacement_table_flag,
                do_add_columns=do_add_columns,
                use_replicated_merge_tree=use_replicated_merge_tree,
                use_json_feature=use_json_feature,
                use_subsampling_feature=use_subsampling_feature,
                use_shared_merge_tree=use_shared_merge_tree,
                organisations=organisations,
                logger=logger,
                target_config_data=target_config_data,
                schema_filter_list=args_schema_filter_list,
                derived_schema_filter_list=args_derived_schema_filter_list,
                schema_filter_wildchar=args_schema_filter_wildchar,
                derived_schema_filter_wildchar=args_derived_schema_filter_wildchar,
                max_insert_threads=args_max_insert_threads,
                min_insert_block_size_rows=args_min_insert_block_size_rows,
            )
            results = schema_executor.process_sql_scripts(is_api_call=is_api_call)
            logger.info("Schema Modification completed successfully.")
            logger.info("Apply Schema Ran with this target:")
            DFEConfigLoader.print_target(
                logger=logger,
                target_name=args_target,
                targets_file_path=args_target_file_path,
            )
            if is_api_call:
                return results

        except Exception as e:
            logger.error(
                f"An error occurred while modifying schemas: {e}. Please check your schema configurations and ensure that the specified schemas exist and are valid. If the issue persists, refer to the logs for more detailed information."
            )
            if is_api_call:
                return [
                    {
                        "status": "error",
                        "error_message": f"An error occurred while modifying schemas: {e}",
                    }
                ]
            raise

    @staticmethod
    def apply_schemas(
        args_dfe_package_file_path: str,
        args_schema_directory: str,
        args_do_add_roles: bool,
        args_use_json_feature: bool,
        args_log_path: str,
        args_target: str,
        args_target_file_path: str,
        args_schema_filter_list: str = None,
        args_derived_schema_filter_list: str = None,
        args_schema_filter_wildchar: str = None,
        args_derived_schema_filter_wildchar: str = None,
        args_org_filter_list: str = None,
        verbose: bool = False,
    ) -> None:
        """
        Apply schema configurations and execute SQL scripts.

        Parameters:
            dfe_package_file_path (str): Path to the DFE package configuration file.
            args_schema_directory (str): Name of the schema config directory in the .dfe_schema folder.
            args_do_add_roles (bool): Apply HyperSec core roles for RBAC.
            args_log_path (str): Path to the logging directory.
            args_target (str): Target name for the specific environment.
            args_target_file_path (str): Path to the target's configuration file.
            args_schema_filter_list (str): A key to lookup for which schemas to apply [List based].
            args_derived_schema_filter_list (str): A key to lookup for which sub-schemas to apply [List based].
            args_schema_filter_wildchar (str): A key to lookup for which schemas to apply [wildchar based].
            args_derived_schema_filter_wildchar (str): A key to lookup for which sub-schemas to apply [wildchar based].
            args_org_filter_list (str): A key to lookup for which customers to update schemas for.
            args_use_json_feature (bool): Using JSON Feature.
            verbose (bool): For detailed logging.
        """

        with SchemaController.lock:
            try:
                dfe_config = DFEConfigLoader.load_dfe_package(
                    config_file_path=args_dfe_package_file_path
                )
                dfe_config_target_path = dfe_config["global_settings"].get("target_path", None)
            except FileNotFoundError as error:
                logger.error(f"Error: {error}")
                return

        try:
            target_path = args_target_file_path if args_target_file_path else dfe_config_target_path
            target_config_data = DFEConfigLoader.read_target_config(
                target_name=args_target, targets_file_path=target_path
            )

        except (FileNotFoundError, Exception) as error:
            logger.error(f"Unable to load target: {error}", exc_info=True)
            sys.exit(1)

        do_add_roles = args_do_add_roles or dfe_config["apply_schemas"].get("do_add_roles", False)
        use_json_feature = args_use_json_feature or dfe_config["global_settings"].get(
            "use_json_feature", False
        )
        schema_directory_str = (
            args_schema_directory or dfe_config["global_settings"]["derived_schema_paths"]
        )
        Path(schema_directory_str)
        dfe_output_path = Path(dfe_config["global_settings"]["schema_output_path"])

        if not dfe_output_path.exists():
            logger.error(
                "The '.dfe_schema_output' directory does not exist in the expected location."
            )
            return

        organisations = dfe_config.get("organisations", [])

        if args_org_filter_list:
            organisation_keys = set(args_org_filter_list.split(","))
            organisations = [org for org in organisations if org.get("org_id") in organisation_keys]
            logger.info(f"Filtered organisations: {organisations}")
        else:
            organisations = dfe_config.get("organisations", [])

        try:
            if not organisations:
                temp_error_message = ""
                if args_org_filter_list:
                    temp_error_message = f"with key '{args_org_filter_list}' "
                raise Exception(
                    f"Error: No organisations found. {temp_error_message} This may occur if the configuration is missing or does not include any organisation entries. Please ensure that your configuration is correctly set up before running this command."
                )

            schema_executor = SchemaExecutor(
                dfe_output_directory=dfe_output_path,
                organisations=organisations,
                do_add_roles=do_add_roles,
                logger=logger,
                target_config_data=target_config_data,
                schema_filter_list=args_schema_filter_list,
                derived_schema_filter_list=args_derived_schema_filter_list,
                schema_filter_wildchar=args_schema_filter_wildchar,
                derived_schema_filter_wildchar=args_derived_schema_filter_wildchar,
                use_json_feature=use_json_feature,
            )
            schema_executor.run_sql_scripts()
        except Exception as e:
            logger.error(f"An error occurred: {e}", exc_info=True)

        logger.info("Apply Schema Ran with this target:")
        DFEConfigLoader.print_target(
            logger=logger,
            target_name=args_target,
            targets_file_path=args_target_file_path,
        )

    @staticmethod
    def get_resource_path(logger: Any, package: str, resource_path: str):
        """
        Gets the full path of the resource within a package.
        """
        try:
            return resources.files(package) / resource_path
        except FileNotFoundError:
            logger.error(f"Resource does not exist: {package}/{resource_path}")
            return None

    @staticmethod
    def read_all_schemas_from_directory(logger: Any, directory_path: Path) -> List[dict[str, str]]:
        meta_schemas = []

        if not directory_path.exists() or not directory_path.is_dir():
            logger.error(f"The directory '{directory_path}' does not exist or is not a directory.")
            return meta_schemas

        for schema_dir in directory_path.iterdir():
            if schema_dir.is_dir():
                for version_dir in schema_dir.iterdir():
                    if version_dir.is_dir():
                        for file in version_dir.iterdir():
                            if file.is_file():
                                schema_name = schema_dir.name
                                template_resource = file.name
                                template_version = SchemaUtils.normalize_dot_version(
                                    version_dir.name
                                )
                                template_resource_path = (
                                    f"{schema_name}/{template_version}/{template_resource}"
                                )

                                meta_schemas.append(
                                    {
                                        "schema_name": schema_name,
                                        "template_resource": template_resource,
                                        "template_version": version_dir.name,
                                        "resource_path": template_resource_path,
                                        "resource_full_path": str(file),
                                    }
                                )

        return meta_schemas

    @staticmethod
    def read_common_types_from_package(
        logger: Any, dfe_package_file_path: str
    ) -> List[dict[str, str]]:
        meta_schemas = []

        try:
            config = DFEConfigLoader.load_dfe_package(config_file_path=dfe_package_file_path)
            common_version = SchemaUtils.normalize_dot_version(
                config["global_settings"]["schema_common_version"]
            )
            common_resource_path = f"common/{common_version}/type_maps.csv"

        except FileNotFoundError as error:
            logger.error(
                f"Error loading dfe_package and pull the common schema version to descirbe the types: {error}",
                exc_info=True,
            )
            return meta_schemas

        try:
            resource_full_path = (
                resources.files(SchemaBuilder.COMMON_RESOURES_PACKAGE_NAME) / common_resource_path
            )
            if resource_full_path.exists():
                meta_schemas.append(
                    {
                        "schema_name": "common",
                        "template_resource": "type_maps.csv",
                        "template_version": common_version,
                        "resource_path": common_resource_path,
                        "resource_full_path": str(
                            resource_full_path
                        ),  # Ensure to convert Path to str
                    }
                )
        except FileNotFoundError:
            logger.error(
                f"Resource does not exist: {SchemaBuilder.COMMON_RESOURES_PACKAGE_NAME}/{common_resource_path}"
            )

        return meta_schemas

    @staticmethod
    def download_meta_schemas(
        args_dfe_package_file_path: str, args_log_path: str, args_output_zip: str
    ) -> None:
        csv_files = []

        try:
            dfe_config_data = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path
            )
        except FileNotFoundError as error:
            logger.error(f"Error: {error}", exc_info=True)
            return

        meta_schema_path = dfe_config_data.get("global_settings", {}).get("meta_schema_paths", None)
        meta_schema_path = Path(meta_schema_path)
        if not meta_schema_path:
            raise ValueError(
                "meta_schema_paths is missing from the configuration. Please provide it under 'global_settings'."
            )

        schema_resources = SchemaController.read_all_schemas_from_directory(
            logger, meta_schema_path
        )
        for resource in schema_resources:
            csv_files.append(resource["resource_full_path"])

        with ZipFile(args_output_zip, "w") as zipf:
            for file in csv_files:
                file_parts = file.split(os.sep)
                schema_name = file_parts[-3]
                version = file_parts[-2]
                filename = os.path.basename(file)
                rel_path = os.path.join(schema_name, version, filename)

                zipf.write(file, rel_path)
            logger.info(f"Successfully zipped CSVs into {args_output_zip}")

    @staticmethod
    def list_meta_schemas(
        args_log_path: str,
        args_dfe_package_file_path: str,
    ) -> None:
        try:
            dfe_config_data = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path
            )
        except FileNotFoundError as error:
            logger.error(f"Error: {error}", exc_info=True)
            return

        meta_schema_path = dfe_config_data.get("global_settings", {}).get("meta_schema_paths", None)
        meta_schema_path = Path(meta_schema_path)

        if not meta_schema_path:
            raise ValueError(
                "meta_schema_paths is missing from the configuration. Please provide it under 'global_settings'."
            )

        meta_schemas = SchemaController.read_all_schemas_from_directory(logger, meta_schema_path)

        if meta_schemas:
            formatted_schemas = [
                [
                    schema["schema_name"],
                    schema["template_resource"],
                    schema["template_version"].replace("_", "."),  # Replace underscores with dots
                    schema["resource_path"],
                ]
                for schema in meta_schemas
            ]
            headers = [
                "Schema Name",
                "Template Resource",
                "Template Version",
                "Resource Path",
            ]
            logger.info(
                "\nHyperSec DFE - List of Core Schemas\n"
                + tabulate(formatted_schemas, headers=headers, tablefmt="grid")
            )
        else:
            logger.info("No meta schemas  found.")

    @staticmethod
    def describe_dfe_clickhouse_schema_types(
        args_log_path: str, args_dfe_package_file_path: str
    ) -> None:
        meta_schemas = SchemaController.read_common_types_from_package(
            logger=logger, dfe_package_file_path=args_dfe_package_file_path
        )

        if not meta_schemas:
            logger.error("No meta schemas  found.")
            return

        csv_file_path = meta_schemas[0]["resource_full_path"]

        selected_columns = ["type", "clickhouse_type", "comment"]
        display_columns = ["DFE Schema Type", "Clickhouse Type", "Description"]

        try:
            df = pd.read_csv(csv_file_path)
            df_selected = df[selected_columns]
            df_selected.columns = display_columns

            header = "HyperSec DFE - Schema Types"
            logger.info("\n" + header)
            logger.info("\n" + tabulate(df_selected, headers="keys", tablefmt="grid"))

        except FileNotFoundError:
            logger.error(f"The file {csv_file_path} does not exist.")
        except KeyError as e:
            logger.error(f"Missing expected column in CSV file: {e}")
        except Exception as e:
            logger.error(f"An error occurred: {e}")

    @staticmethod
    def list_schema_fields(
        args_dfe_package_file_path: str,
        args_schema_name: str,
        args_template_version: str,
        args_log_path: str,
    ) -> None:
        try:
            dfe_config_data = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path
            )
        except FileNotFoundError as error:
            logger.error(f"Error: {error}", exc_info=True)
            return

        meta_schema_path = dfe_config_data.get("global_settings", {}).get("meta_schema_paths", None)
        meta_schema_path = Path(meta_schema_path)

        meta_schemas = SchemaController.read_all_schemas_from_directory(logger, meta_schema_path)

        matched_resources = [
            resource
            for resource in meta_schemas
            if resource["schema_name"] == args_schema_name
            and resource["template_version"] == args_template_version.replace(".", "_")
        ]

        if not matched_resources:
            logger.error(
                f"No resources found for schema '{args_schema_name}' with version '{args_template_version}'"
            )
            return

        fields = []
        for resource in matched_resources:
            full_path = resource["resource_full_path"]
            with open(full_path, "r") as file:
                for line in file:
                    fields.append(
                        line.strip().split(",")
                    )  # Assuming CSV format where each line represents a field

        header = f"HyperSec DFE - Fields for {args_schema_name} (Version {args_template_version})"
        logger.info("\n" + header)
        logger.info("\n" + tabulate(fields, headers="firstrow", tablefmt="grid"))

    @staticmethod
    def check_db_table(
        args_dfe_package_file_path,
        args_target_file_path,
        args_log_path,
        database_name: str,
        table_name: str,
    ) -> bool:
        """
        Check if database and table already exists on clickhouse

        Args:
            database_name (str): Name of the database.
            table_name (str): Name of the table.

        Returns:
            bool value
        """
        with SchemaController.lock:
            try:
                dfe_config = DFEConfigLoader.load_dfe_package(
                    config_file_path=args_dfe_package_file_path
                )
                dfe_config_target_path = dfe_config["global_settings"].get("target_path", None)
            except FileNotFoundError as error:
                logger.error(f"Error: {error}")
                return

        target_path = args_target_file_path if args_target_file_path else dfe_config_target_path
        target_config_data = DFEConfigLoader.read_target_config(
            target_name="hypersec", targets_file_path=target_path
        )
        clickhouse_manager = ClickHouseManager.get_instance(target_config_data=target_config_data)
        query = f"SELECT name FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}'"
        with clickhouse_manager.get_clickhouse_client() as ch_client:
            result = ch_client.execute(query)
            if result:
                return True
            else:
                return False
