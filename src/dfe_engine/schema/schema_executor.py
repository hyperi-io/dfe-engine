import os
import re

from jinja2 import Environment, FileSystemLoader
from ..clickhouse.clickhouse_manager import ClickHouseManager
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional
from .schema_util import SchemaUtils
from hs_lib.logger import logger


class SchemaExecutor:
    def __init__(
        self,
        dfe_output_directory: str,
        organisations: list,
        do_add_roles: bool = False,
        use_json_feature: bool = False,
        use_subsampling_feature: bool = False,
        target_config_data: Optional[dict] = None,
        schema_filter_list: Optional[str] = None,
        derived_schema_filter_list: Optional[str] = None,
        schema_filter_wildchar: Optional[str] = None,
        derived_schema_filter_wildchar: Optional[str] = None,
    ):
        """
        Initialize the SchemaExecutor instance.

        Args:
            dfe_output_directory (str): Path to the DFE output directory.
            organisations (Dictionary): Dictionary containing customer data.
            do_add_roles (bool, optional): Whether to add roles. Defaults to False.
        """
        self.dfe_output_directory = dfe_output_directory
        self.organisations = organisations
        self.do_add_roles = do_add_roles
        self.target_config_data = target_config_data
        self.clickhouse_manager = ClickHouseManager.get_instance(
            target_config_data=target_config_data
        )
        self.schema_filter_list = schema_filter_list
        self.derived_schema_filter_list = derived_schema_filter_list
        self.schema_filter_wildchar = schema_filter_wildchar
        self.derived_schema_filter_wildchar = derived_schema_filter_wildchar
        self.use_json_feature = use_json_feature
        self.use_subsampling_feature = use_subsampling_feature

    def process_sql_template(self, sql_template: str, org_id: str) -> str:
        """
        Process the SQL template with customer data.

        Args:
            sql_template (str): SQL template string.
            org_id (str): Organization ID.

        Returns:
            str: Processed SQL command.
        """
        env = Environment(loader=FileSystemLoader("/"), autoescape=True)
        template = env.from_string(sql_template)
        return template.render(org_id=org_id)

    def run_create_roles(self) -> None:
        """
        Executes SQL statements to create roles in ClickHouse directly without creating a file.

        If any exceptions occur during the execution, they are logged, and the method raises an error.

        Raises:
            Exception: If any error occurs during the execution of create roles statements.
        """
        try:
            logger.info("Creating roles directly")

            roles_sql = []
            for organisation in self.organisations:
                org_id = organisation.get("org_id", "default_orgid")
                roles_sql.extend(
                    [
                        f"-- Roles for {org_id}",
                        "CREATE ROLE IF NOT EXISTS read_only_role;",
                        f"GRANT SHOW, SELECT ON {org_id}.* TO read_only_role;",
                        "CREATE ROLE IF NOT EXISTS read_write_role;",
                        f"GRANT SHOW, SELECT, INSERT, OPTIMIZE ON {org_id}.* TO read_write_role;",
                        "CREATE ROLE IF NOT EXISTS loader_role;",
                        f"GRANT INSERT ON {org_id}.* TO loader_role;",
                        "CREATE ROLE IF NOT EXISTS detections_role;",
                        f"GRANT SELECT ON {org_id}.* TO detections_role;",
                        f"GRANT INSERT ON {org_id}.logs_alerts TO detections_role;",
                        "REVOKE INSERT, ALTER ON system.* FROM detections_role;",
                        "GRANT CREATE, ALTER ON xdr_audit.* TO detections_role;",
                        "GRANT INSERT, UPDATE, DELETE ON xdr_audit.* TO detections_role;",
                        "",
                    ]
                )

            sql_statements = [
                s.strip() for s in roles_sql if s.strip() and not s.strip().startswith("--")
            ]
            logger.debug(f"SQL role statements to run: {len(sql_statements)} statements found")

            with self.clickhouse_manager.get_clickhouse_client() as ch_client:
                for sql in sql_statements:
                    if sql:
                        try:
                            result = ch_client.execute(sql)
                            logger.info(f"Executed role statement: {sql} \n Result: \n{result}")
                        except Exception as e:
                            logger.warning(
                                f"An error occurred while executing Roles in SQL: {sql}. Error: {e}"
                            )
                            continue

        except FileNotFoundError as e:
            logger.error(f"File not found: {e}")
        except Exception as e:
            logger.error(f"An error occurred while executing create roles statements: {e}")
            raise

    def run_create_database(self) -> None:
        """
        Executes SQL statements to create databases in ClickHouse directly without creating a file.

        If any exceptions occur during the execution, they are logged, and the method raises an error.

        Raises:
            Exception: If any error occurs during the execution of create database statements.
        """
        try:
            logger.info("Creating databases directly")

            create_database_statements = []
            for organisation in self.organisations:
                org_id = organisation.get("org_id", "default_orgid")
                cluster_name = organisation.get("cluster_name", "default_cluster")

                no_cluster_declarations_needed = self.target_config_data.get(
                    "no_cluster_declarations_needed", True
                )

                if no_cluster_declarations_needed:
                    create_statement = f"CREATE DATABASE IF NOT EXISTS {org_id};"
                else:
                    create_statement = (
                        f"CREATE DATABASE IF NOT EXISTS {org_id} ON CLUSTER {cluster_name};"
                    )

                create_database_statements.append(create_statement)
                logger.info(f"Database statement prepared: {create_statement}")

            with self.clickhouse_manager.get_clickhouse_client() as ch_client:
                for sql in create_database_statements:
                    if sql:
                        db_name = (
                            sql.split("CREATE DATABASE IF NOT EXISTS ")[1]
                            .split(";")[0]
                            .split(" ")[0]
                        )

                        result = ch_client.execute(sql)
                        logger.info(f"Executed database statement: {sql} \n Result: {result}")

                        verify_query = f"SELECT name FROM system.databases WHERE name = '{db_name}'"
                        verify_result = ch_client.execute(verify_query)

                        if verify_result:
                            logger.info(f"✅ Database '{db_name}' exists and is accessible.")
                        else:
                            logger.warning(
                                f"⚠️ Database '{db_name}' could not be verified after creation!"
                            )

        except Exception as e:
            logger.error(f"An error occurred while executing create database statements: {e}")
            raise

    def run_sql_scripts(self):
        """
        Orchestrates the execution of SQL scripts for creating databases, tables, and roles.
        """
        try:
            logger.info("Starting SQL script execution.")

            logger.info("Step 1: Creating databases for all organizations")
            self.run_create_database()

            logger.info("Step 2: Creating tables from SQL scripts")
            self.process_sql_scripts()

            if self.do_add_roles:
                logger.info("Step 3: Creating and assigning roles")
                self.run_create_roles()
            else:
                logger.info(
                    "Step 3: Skipped - Roles were not requested to be applied to ClickHouse."
                )
        except Exception as e:
            logger.error(f"An error occurred during SQL script execution. {e}", exc_info=True)
        finally:
            self.clickhouse_manager.cleanup()
            logger.debug("SQL script execution completed and resources cleaned up.")

    def process_sql_scripts(self) -> None:
        """
        Processes and executes SQL scripts based on the provided customer data.
        """
        try:
            customer_data = SchemaUtils.read_customer_list(
                organisations=self.organisations, logger=logger
            )
            logger.info(f"customer data {customer_data}")
            schema_files = set(self.collect_schema_files(customer_data))
            logger.info(f"schema files {schema_files}")
            # Execute SQL files concurrently
            with ThreadPoolExecutor() as executor:
                futures = {
                    executor.submit(self.execute_sql_files, org_id, root, file_name): (
                        org_id,
                        root,
                        file_name,
                    )
                    for org_id, root, file_name in schema_files
                }
                for future in as_completed(futures):
                    sql_file = futures[future]
                    try:
                        future.result()
                        logger.info(f"Successfully executed SQL file: {sql_file}")
                    except Exception as exc:
                        logger.error(f"SQL file {sql_file} generated an exception: {exc}")
        except Exception as e:
            logger.error(
                f"An error occurred while processing the SQL scripts: {e}",
                exc_info=True,
            )

    def collect_schema_files(self, customer_data: dict) -> List[Tuple[str, str, str]]:
        """
        Collect schema files based on the provided customer data and filtered schema keys.

        Parameters:
            customer_data (dict): Customer data dictionary.

        Returns:
            List[Tuple[str, str, str]]: List of tuples containing (org_id, root, file_name).
        """
        schema_files = []
        for org_id, _data in customer_data.items():
            for root, _, files in SchemaUtils.walk_schema_directory(
                dfe_output_directory=self.dfe_output_directory
            ):
                schema_files.extend(
                    SchemaUtils.filter_schema_files(
                        org_id,
                        root,
                        files,
                        self.schema_filter_list,
                        self.derived_schema_filter_list,
                        self.schema_filter_wildchar,
                        self.derived_schema_filter_wildchar,
                    )
                )

        if not schema_files:
            return []

        logger.info(f"Using the following list of schemas: '{schema_files}'.")
        return schema_files

    def parse_table_name(self, ddl: str) -> tuple:
        """
        Parse the database and table names from the DDL.

        Args:
            ddl (str): DDL statement for creating the table.

        Returns:
            tuple: A tuple containing the database name and table name.
        """
        match = re.search(r"CREATE TABLE IF NOT EXISTS ([\w\.]+)\.(\w+)", ddl)
        if match:
            database_name = match.group(1)
            table_name = match.group(2)
            return database_name, table_name
        raise ValueError("Database and table names could not be extracted from the DDL")

    def schema_existence_check(self, database_name: str, table_name: str) -> list:
        """
        Fetch existing columns from the database.

        Args:
            database_name (str): Name of the database.
            table_name (str): Name of the table.

        Returns:
            list: A list of existing column names.
        """
        query = f"SELECT name FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}'"
        with self.clickhouse_manager.get_clickhouse_client() as ch_client:
            result = ch_client.execute(query)
            if result:
                try:
                    describe_query = f"DESCRIBE TABLE {database_name}.{table_name}"
                    describe_result = ch_client.execute(describe_query)
                    columns = [row[0] for row in describe_result]
                    return columns
                except Exception as e:
                    logger.warning(f"Failed to describe table {database_name}.{table_name}: {e}")
                    return []
            else:
                logger.info(f"Table {database_name}.{table_name} does not exist.")
                return []

    def table_exists(self, database_name: str, table_name: str) -> bool:
        """
        Check if a table exists in the database.

        Args:
            database_name (str): Name of the database.
            table_name (str): Name of the table.

        Returns:
            bool: True if table exists, False otherwise.
        """
        query = f"SELECT name FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}'"
        with self.clickhouse_manager.get_clickhouse_client() as ch_client:
            result = ch_client.execute(query)
            return bool(result)

    def execute_sql_files(self, org_id: str, root: str, file_name: str) -> None:
        """
        Execute SQL files with optional JSON feature handling.

        Args:
            org_id (str): Organization ID.
            root (str): Root directory path.
            file_name (str): Name of the SQL file.
        """
        sql_file_path = os.path.join(root, file_name)
        logger.debug(f"\n\n*** Executing SQL files for org_id: {org_id} ***\n\n")

        try:
            with open(sql_file_path, "r", encoding="utf-8") as ddl_file:
                ddl_content = ddl_file.read()
                sql_command = self.process_sql_template(ddl_content, org_id)
                with self.clickhouse_manager.get_clickhouse_client() as ch_client:
                    # Enable experimental JSON type only if use_json_feature flag is True
                    if self.use_json_feature:
                        set_json_type_command = "SET allow_experimental_json_type = 1;"
                        ch_client.execute(set_json_type_command)
                        logger.debug(f"Executed: {set_json_type_command}")
                    else:
                        logger.debug(
                            "Skipping experimental JSON feature, running apply-schema without it."
                        )

                    if SchemaUtils.is_view(ddl_content):
                        result = ch_client.execute(sql_command)
                    else:
                        database_name, table_name = self.parse_table_name(sql_command)
                        schema_exists = self.schema_existence_check(database_name, table_name)

                        if not schema_exists:
                            logger.info(
                                f"Table {database_name}.{table_name} does not exist. Creating new table."
                            )
                            logger.debug(f"Executing SQL: [{sql_command}] for org_id: {org_id}")
                            result = ch_client.execute(sql_command)
                            logger.debug(f"Executed {sql_file_path}. Result: {result}")

                            # Verify table was actually created
                            table_created = self.table_exists(database_name, table_name)
                            if not table_created:
                                raise Exception(
                                    f"Table {database_name}.{table_name} was not created successfully despite successful SQL execution"
                                )
                            logger.info(f"Table {database_name}.{table_name} created successfully.")
                        else:
                            logger.warning(
                                f"Table {database_name}.{table_name} already exists. No action performed."
                            )

        except Exception as e:
            logger.error(
                f"Failed to execute SQL from {sql_file_path} for org_id {org_id}. Error: {e}",
                exc_info=True,
            )
            raise
