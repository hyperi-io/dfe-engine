from pydantic import BaseModel
from typing import List, Tuple, Optional
import re
import os
import json
from pathlib import Path
from datetime import datetime

from hs_lib.logger import logger
from ..clickhouse.clickhouse_manager import ClickHouseManager
from ..clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler
from .schema_util import SchemaUtils


class TableStats(BaseModel):
    table_name: str
    size_bytes: int
    total_rows: int
    rows_by_day: List[Tuple[datetime, int]]


class SchemaDiff(BaseModel):
    table_name: str
    current_primary_key: Optional[str]
    expected_primary_key: Optional[str]
    current_order_by_key: Optional[str]
    expected_order_by_key: Optional[str]
    current_sample_by: Optional[str] = None
    expected_sample_by: Optional[str] = None
    current_indexes: List[Tuple[str, str]]
    expected_indexes: List[Tuple[str, str]]


class SchemaDiffDetails(BaseModel):
    current_primary_key: Optional[str]
    expected_primary_key: Optional[str]
    current_order_by_key: Optional[str]
    expected_order_by_key: Optional[str]
    current_sample_by: Optional[str] = None
    expected_sample_by: Optional[str] = None
    current_indexes: List[Tuple[str, str]]
    expected_indexes: List[Tuple[str, str]]


class SchemaDiffResult(BaseModel):
    schema_difference: bool
    schema_diff: SchemaDiffDetails


class SchemaPlan:
    def __init__(
        self,
        dfe_output_directory: str,
        organisations: list,
        schema_filter_list: str = None,
        derived_schema_filter_list: str = None,
        schema_filter_wildchar: str = None,
        derived_schema_filter_wildchar: str = None,
        target_config_data: dict = None,
        logger = None,
    ):
        """
        Initialize the SchemaPlan with required parameters.
        """
        self.dfe_output_directory = dfe_output_directory
        self.organisations = organisations
        self.schema_filter_list = schema_filter_list
        self.derived_schema_filter_list = derived_schema_filter_list
        self.schema_filter_wildchar = schema_filter_wildchar
        self.derived_schema_filter_wildchar = derived_schema_filter_wildchar
        self.schema_update_map = {}
        self.target_config_data = target_config_data
        self.clickhouse_manager = ClickHouseManager.get_instance(
            target_config_data=target_config_data
        )
        self.ch_client = self.clickhouse_manager.get_clickhouse_client()

    def process_sql_scripts(self, is_api_call: bool= False) -> List[dict]:
        """
        Processes and executes SQL scripts based on the provided customer data.
        
        Parameters:
            is_api_call (bool, optional): If True, return results as structured data for API use. Defaults to False.
            
        Returns:
            List[Dict]: If is_api_call is True, returns a list of structured results. Otherwise returns None.
        """
        try:
            customer_data = SchemaUtils.read_customer_list(
                organisations=self.organisations, logger=logger
            )
            schema_files = self.collect_schema_files(customer_data)
            return self.execute_schema_files(schema_files, is_api_call=is_api_call)
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(
                f"An error occurred while processing the SQL scripts: {parsed_error['user_message']}",
                exc_info=True,
            )
            if is_api_call:
                return [{"status": "error", "error_message": parsed_error['user_message']}]
            return None

    def collect_schema_files(self, customer_data: dict) -> List[Tuple[str, str, str]]:
        """
        Collect schema files based on the provided customer data and filtered schema keys.

        Parameters:
            customer_data (dict): Customer data dictionary.

        Returns:
            List[Tuple[str, str, str]]: List of tuples containing (org_id, root, file_name).
        """
        schema_files = []
        for org_id, data in customer_data.items():
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
            logger.info(f"Using the following list of schemas: '{schema_files}'.")
            return []

        logger.info(f"Using the following list of schemas: '{schema_files}'.")
        return schema_files

    def execute_schema_files(self, schema_files: List[Tuple[str, str, str]], is_api_call: bool) -> List[dict]:
        """
        Execute the collected schema files.

        Parameters:
            schema_files (List[Tuple[str, str, str]]): List of tuples containing (org_id, root, file_name).
            is_api_call (bool): If True, return results as structured data for API use.

        Returns:
            List[Dict]: If is_api_call is True, returns a list of structured results. Otherwise returns None.
        """
        results = [] if is_api_call else None
        for org_id, root, file_name in schema_files:
            sql_file_path = os.path.join(root, file_name)
            table_name = Path(file_name).stem
            database_name = org_id
            with open(sql_file_path, "r", encoding="utf-8") as ddl_file:
                ddl_content = ddl_file.read()
                if SchemaUtils.is_view(ddl_content):
                    logger.warning(
                        f"This is a View {table_name} so it will be skipped from processing"
                    )
                    continue
                result = self.plan_schemas(ddl_content, database_name, table_name, is_api_call=is_api_call)
                if is_api_call and result:
                    results.append(result)
        return results

    @staticmethod
    def default_json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    def parse_columns_from_ddl(self, ddl: str) -> list:
        """
        Parse column names from the DDL.

        Args:
            ddl (str): DDL statement for creating the table.

        Returns:
            list: A list of column names.
        """
        columns_part = re.search(r"\((.*?)\)\s*ENGINE", ddl, re.DOTALL).group(1)
        columns = []
        current_column = ""
        in_column_definition = False

        in_projection = False
        projection_paren_count = 0
        
        for line in columns_part.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            if line.endswith(","):
                line = line[:-1]

            if line.startswith("INDEX "):
                continue
            
            
            if line.startswith("PROJECTION "):
                in_projection = True
                projection_paren_count = line.count("(") - line.count(")")
                continue
            
            if in_projection:
                projection_paren_count += line.count("(") - line.count(")")
                if projection_paren_count <= 0:
                    in_projection = False
                    projection_paren_count = 0
                continue

            if not in_column_definition:
                current_column = line
                if "(" in line and ")" not in line:
                    in_column_definition = True
            else:
                current_column += " " + line
                if ")" in line:
                    in_column_definition = False
            if not in_column_definition:
                if current_column:
                    column_name = current_column.split()[0]
                    columns.append(column_name)
                current_column = ""

        return columns

    def fetch_existing_columns(self, database_name: str, table_name: str) -> list:
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
                    parsed_error = ClickHouseErrorHandler.parse_error(e)
                    logger.warning(
                        f"Failed to describe table {database_name}.{table_name}: {parsed_error['user_message']}"
                    )
                    return []
            else:
                logger.warning(
                    f"Table {database_name}.{table_name} does not exist."
                )
                return []
    def plan_schemas(self, ddl_statement: str, database_name: str, table_name: str, is_api_call: bool = False):
        """
        Plan Schemas to display the schema drift, including table size information
        and column differences (columns to be added).
        
        Args:
            ddl_statement (str): The DDL statement to plan.
            database_name (str): The database name to plan for.
            table_name (str): The table name to plan for.
            is_api_call (bool, optional): If True, return the results as structured data instead of just logging.
                                          For FastAPI endpoint use. Defaults to False.
        
        Returns:
            dict: If is_api_call is True, returns a structured dict with plan results.
                  Otherwise returns None.
        """
        api_result = None
        if is_api_call:
            api_result = {
                "schema_name": table_name,
                "organisation_id": database_name,
                "database_name": database_name,
                "table_name": table_name,
                "status": "unknown",
                "differences": {
                    "table_name": table_name,
                    "has_changes": False,
                    "change_summary": "",
                    "current_primary_key": None,
                    "expected_primary_key": None,
                    "current_order_by_key": None,
                    "expected_order_by_key": None,
                    "current_sample_by": None,
                    "expected_sample_by": None,
                    "current_indexes": [],
                    "expected_indexes": [],
                    "columns_to_add": [],
                    "data_type_changes": [],
                    "table_size_bytes": 0,
                    "total_rows": 0
                }
            }

        target_name = self.target_config_data.get('target_name', 'No target name specified')
        logger.info(f"\n\nPlanning schema for table: {table_name} @ in target [{target_name}]")

        try:
            databases = self.ch_client.execute(f"SHOW DATABASES LIKE '{database_name}'")
            logger.info(f" found databases [{databases}]")

            if database_name not in {db[0] for db in databases}:
                logger.info(f"+++ new database to create [{database_name}]")
                if is_api_call:
                    api_result["status"] = "new_table"
                    api_result["differences"]["has_changes"] = True
                    api_result["differences"]["change_summary"] = f"New database {database_name} needs to be created"
                    primary_key, order_by_key, indexes, ttl_value, sample_by, partition_by, projection, table_settings = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl_statement)
                    api_result["differences"]["expected_primary_key"] = primary_key
                    api_result["differences"]["expected_order_by_key"] = order_by_key
                    api_result["differences"]["expected_sample_by"] = sample_by or ""
                    api_result["differences"]["expected_indexes"] = [{"name": idx[0], "definition": idx[1]} for idx in indexes]
                    return api_result
                else:
                    self.print_new_table_structure(database_name, table_name, ddl_statement)
                    return
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error checking databases: {parsed_error['message']}")
            if is_api_call:
                api_result["status"] = "error"
                api_result["error_message"] = parsed_error['user_message']
                api_result["error_details"] = parsed_error['message']
                api_result["error_code"] = parsed_error['code']
                return api_result
            return

        try:
            tables = self.ch_client.execute(f"SHOW TABLES FROM {database_name}")
            if table_name not in {table[0] for table in tables}:
                if is_api_call:
                    api_result["status"] = "new_table"
                    api_result["differences"]["has_changes"] = True
                    api_result["differences"]["change_summary"] = f"New table {table_name} needs to be created"
                    primary_key, order_by_key, indexes, ttl_value, sample_by, partition_by, projection, table_settings = SchemaUtils.extract_keys_and_indexes_from_ddl(ddl_statement)
                    api_result["differences"]["expected_primary_key"] = primary_key
                    api_result["differences"]["expected_order_by_key"] = order_by_key
                    api_result["differences"]["expected_sample_by"] = sample_by or ""
                    api_result["differences"]["expected_indexes"] = [{"name": idx[0], "definition": idx[1]} for idx in indexes]
                    return api_result
                else:
                    self.print_new_table_structure(database_name, table_name, ddl_statement)
                    return
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error checking tables in database {database_name}: {parsed_error['message']}")
            if is_api_call:
                api_result["status"] = "error"
                api_result["error_message"] = parsed_error['user_message']
                api_result["error_details"] = parsed_error['message']
                api_result["error_code"] = parsed_error['code']
                return api_result
            return

        current_schema_ddl = self.get_existing_schema_ddl(database_name, table_name)

        logger.debug(f"**** Current Schema DDL in {current_schema_ddl} /n")
        logger.debug(f"**** Expected Schema DDL in {ddl_statement} /n")
        
        expected_schema_ddl = ddl_statement

        if not current_schema_ddl and not expected_schema_ddl:
            logger.warning(f"Both current and expected schemas are not available for table {table_name}.")
            if is_api_call:
                api_result["status"] = "error"
                api_result["error_message"] = f"Both current and expected schemas are not available for table {table_name}."
                return api_result
            return

        try:
            new_columns = self.parse_columns_from_ddl(expected_schema_ddl)
            existing_columns = self.fetch_existing_columns(database_name, table_name)
        
            columns_to_add = list(set(new_columns) - set(existing_columns))
            logger.info(f"Columns to add: {columns_to_add}")
            
            
            data_type_changes = self._detect_data_type_changes(database_name, table_name, expected_schema_ddl)
            if data_type_changes:
                logger.info(f"Data type changes detected: {data_type_changes}")
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error analyzing columns: {parsed_error['message']}")
            if is_api_call:
                api_result["status"] = "error"
                api_result["error_message"] = parsed_error['user_message']
                api_result["error_details"] = parsed_error['message']
                api_result["error_code"] = parsed_error['code']
                return api_result
            return

        try:
            diff_result = self.detect_schema_differences(database_name, table_name, current_schema_ddl, expected_schema_ddl)
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error detecting schema differences: {parsed_error['message']}")
            if is_api_call:
                api_result["status"] = "error"
                api_result["error_message"] = parsed_error['user_message']
                api_result["error_details"] = parsed_error['message']
                api_result["error_code"] = parsed_error['code']
                return api_result
            return
        
        if not current_schema_ddl:
            logger.info(f"Schema for table {table_name} does not exist. New schema will be created.")
            if is_api_call:
                api_result["status"] = "new_table"
                api_result["differences"]["has_changes"] = True
                api_result["differences"]["change_summary"] = f"New table {table_name} will be created"
        
        try:
            table_size_info = self.capture_current_table_size(database_name, table_name)
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error capturing table size: {parsed_error['message']}")
            if is_api_call:
                api_result["status"] = "error" 
                api_result["error_message"] = parsed_error['user_message']
                api_result["error_details"] = parsed_error['message']
                api_result["error_code"] = parsed_error['code']
                table_size_info = TableStats(
                    table_name=table_name,
                    size_bytes=0,
                    total_rows=0,
                    rows_by_day=[]
                )
        report = []

        if diff_result.schema_difference or columns_to_add or data_type_changes:
            logger.info(f"module.{database_name}.{table_name}: Refreshing schema... [id={table_name}]")

            report.append("\n*******************TABLE STATISTICS******************************\n")
            report.append(f"- Table Size: {table_size_info.size_bytes} bytes")
            report.append(f"- Total rows: {table_size_info.total_rows}")
            report.append(f"- Rows by day: {json.dumps(table_size_info.rows_by_day, indent=4, default = self.default_json_serializer)}")

            if is_api_call:
                api_result["status"] = "changes_detected"
                api_result["differences"]["has_changes"] = True
                api_result["differences"]["table_size_bytes"] = table_size_info.size_bytes
                api_result["differences"]["total_rows"] = table_size_info.total_rows

            report.append("- Changes:")
            change_summaries = []
            if diff_result.schema_diff:
                if diff_result.schema_diff.current_primary_key != diff_result.schema_diff.expected_primary_key:
                    report.append(f"    * Primary Key: {diff_result.schema_diff.current_primary_key} -> {diff_result.schema_diff.expected_primary_key}")
                    if is_api_call:
                        api_result["differences"]["current_primary_key"] = diff_result.schema_diff.current_primary_key
                        api_result["differences"]["expected_primary_key"] = diff_result.schema_diff.expected_primary_key
                        change_summaries.append(f"Primary Key: {diff_result.schema_diff.current_primary_key} -> {diff_result.schema_diff.expected_primary_key}")
                        
                if diff_result.schema_diff.current_order_by_key != diff_result.schema_diff.expected_order_by_key:
                    report.append(f"    * Order By Key: {diff_result.schema_diff.current_order_by_key} -> {diff_result.schema_diff.expected_order_by_key}")
                    if is_api_call:
                        api_result["differences"]["current_order_by_key"] = diff_result.schema_diff.current_order_by_key
                        api_result["differences"]["expected_order_by_key"] = diff_result.schema_diff.expected_order_by_key
                        change_summaries.append(f"Order By Key: {diff_result.schema_diff.current_order_by_key} -> {diff_result.schema_diff.expected_order_by_key}")
                        
                if diff_result.schema_diff.current_sample_by != diff_result.schema_diff.expected_sample_by:
                    current_sample_display = f'""' if diff_result.schema_diff.current_sample_by == "" else diff_result.schema_diff.current_sample_by
                    expected_sample_display = f'""' if diff_result.schema_diff.expected_sample_by == "" else diff_result.schema_diff.expected_sample_by
                    report.append(f"    * Sample By: {current_sample_display} -> {expected_sample_display}")
                    if is_api_call:
                        api_result["differences"]["current_sample_by"] = diff_result.schema_diff.current_sample_by
                        api_result["differences"]["expected_sample_by"] = diff_result.schema_diff.expected_sample_by
                        change_summaries.append(f"Sample By: {current_sample_display} -> {expected_sample_display}")
                
                index_changes = self.summarize_changes(
                    diff_result.schema_diff.current_primary_key,
                    diff_result.schema_diff.expected_primary_key,
                    diff_result.schema_diff.current_indexes,
                    diff_result.schema_diff.expected_indexes
                )
                report.extend(index_changes)
                
                if is_api_call:
                    api_result["differences"]["current_indexes"] = [{"name": idx[0], "definition": idx[1]} for idx in diff_result.schema_diff.current_indexes]
                    api_result["differences"]["expected_indexes"] = [{"name": idx[0], "definition": idx[1]} for idx in diff_result.schema_diff.expected_indexes]
                    
                    for change in index_changes:
                        if change.strip():
                            change_summaries.append(change.strip().replace("    * ", ""))

                self.create_schema_update_map(
                    table_name,
                    diff_result.schema_diff.current_primary_key,
                    diff_result.schema_diff.expected_primary_key,
                    diff_result.schema_diff.current_order_by_key,
                    diff_result.schema_diff.expected_order_by_key,
                    diff_result.schema_diff.current_sample_by,
                    diff_result.schema_diff.expected_sample_by,
                    diff_result.schema_diff.current_indexes,
                    diff_result.schema_diff.expected_indexes
                )
            
            if columns_to_add:
                report.append(f"    * Columns to Add: {', '.join(columns_to_add)}")
                if is_api_call:
                    api_result["differences"]["columns_to_add"] = columns_to_add
                    change_summaries.append(f"Columns to Add: {', '.join(columns_to_add)}")
            
            if data_type_changes:
                data_type_summary = []
                for change in data_type_changes:
                    data_type_summary.append(f"{change['column']}: {change['current_type']} -> {change['expected_type']}")
                report.append(f"    * Data Type Changes: {', '.join(data_type_summary)}")
                if is_api_call:
                    api_result["differences"]["data_type_changes"] = data_type_changes
                    change_summaries.append(f"Data Type Changes: {len(data_type_changes)} columns")
            
            if is_api_call and change_summaries:
                api_result["differences"]["change_summary"] = "; ".join(change_summaries)
                    
        else:
            logger.info(f"\nmodule.{database_name}.{table_name}: No schema changes detected [id={table_name}]\n")
            report.append("\nNo changes detected in schema.\n")
            if is_api_call:
                api_result["status"] = "no_changes"
                api_result["differences"]["has_changes"] = False
                api_result["differences"]["change_summary"] = "No schema changes detected"

        validate_parts = self.validate_parts_creation(database_name, table_name)
        if validate_parts:
            report.append("\nValidate Parts Creation:")
            for part in validate_parts:
                report.append(f"    * {part}")
        else:
            report.append("\nValidate Parts Creation: No parts found or not applicable.\n")
        
        report.append("\n*************************************************\n")

        report_output = "\n".join(report)
        logger.info(report_output)
        
        if is_api_call:
            return api_result



    def print_new_table_structure(
        self, database_name: str, table_name: str, expected_schema_ddl: str
    ) -> None:
        """
        Print the structure of the new table that is going to be created.
        """
        primary_key, order_by_key, indexes, ttl_value, sample_by, partition_by, projection, table_settings = (
            SchemaUtils.extract_keys_and_indexes_from_ddl(expected_schema_ddl)
        )

        report = []
        report.append(
            "\n******************* new table to create ****************************\n"
        )
        report.append(f"Table {table_name} in database {database_name} does not exist.")
        report.append(f"++Primary Key: {primary_key}")
        report.append(f"++Order By Key: {order_by_key}")
        if sample_by:
            report.append(f"++Sample By: {sample_by}")
        for index_name, index_details in indexes:
            report.append(f"++ Index: {index_name} -> {index_details}")
        if ttl_value:
            report.append(f"++ TTL: {ttl_value} days")
        report.append(
            "\n*****************************************************************\n"
        )

        report_output = "\n".join(report)
        logger.info(report_output)

    def detect_schema_differences(self, database_name, table_name, current_schema_ddl, expected_schema_ddl):
        """
        Detect differences between the current table schema and the expected schema.
        """
        result = SchemaDiffResult(
            schema_difference=False,
            schema_diff=SchemaDiffDetails(
                current_primary_key=None,
                expected_primary_key=None,
                current_order_by_key=None,
                expected_order_by_key=None,
                current_sample_by="",
                expected_sample_by="",
                current_indexes=[],
                expected_indexes=[]
            )
        )

        expected_primary_key, expected_order_by_key, expected_indexes, ttl_value, expected_sample_by, expected_partition_by, expected_projection, expected_table_settings = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_schema_ddl)
        expected_sample_by = expected_sample_by or ""
        
        current_schema_query = f"""
            SELECT primary_key, sorting_key
            FROM system.tables
            WHERE database = '{database_name}' AND name = '{table_name}';
        """
        current_schema = self.ch_client.execute(current_schema_query)

        if current_schema:
            current_primary_key = current_schema[0][0]
            current_order_by_key = current_schema[0][1]
        else:
            current_primary_key = None
            current_order_by_key = None

        if current_schema_ddl:
            _, _, current_indexes, _, current_sample_by, current_partition_by, current_projection, current_table_settings = SchemaUtils.extract_keys_and_indexes_from_ddl(current_schema_ddl)
            current_sample_by = current_sample_by or ""
        else:
            current_sample_by = ""
            current_indexes = []

        def indexes_equal(current_idx, expected_idx):
            """Compare two index lists ignoring order"""
            if len(current_idx) != len(expected_idx):
                return False
            
            current_set = set(current_idx)
            expected_set = set(expected_idx)
            return current_set == expected_set

        schema_difference = (current_primary_key != expected_primary_key or
                            current_order_by_key != expected_order_by_key or
                            not indexes_equal(current_indexes, expected_indexes) or
                            current_sample_by != expected_sample_by)

        result.schema_difference = schema_difference
        result.schema_diff.current_primary_key = current_primary_key
        result.schema_diff.expected_primary_key = expected_primary_key
        result.schema_diff.current_order_by_key = current_order_by_key
        result.schema_diff.expected_order_by_key = expected_order_by_key
        result.schema_diff.current_sample_by = current_sample_by
        result.schema_diff.expected_sample_by = expected_sample_by
        result.schema_diff.current_indexes = current_indexes
        result.schema_diff.expected_indexes = expected_indexes

        return result

    def summarize_changes(
        self, primary_key_changes, order_by_changes, current_indexes, expected_indexes
    ):
        """
        Summarize changes in indexes, including additions, removals, and modifications.
        Format the output consistently with other schema changes.
        """
        index_changes = []

        removed_indexes = [
            idx
            for idx in current_indexes
            if idx[0] not in [e_idx[0] for e_idx in expected_indexes]
        ]

        added_indexes = [
            idx
            for idx in expected_indexes
            if idx[0] not in [c_idx[0] for c_idx in current_indexes]
        ]

        changed_indexes = []
        for c_idx in current_indexes:
            for e_idx in expected_indexes:
                if c_idx[0] == e_idx[0] and c_idx[1] != e_idx[1]:
                    changed_indexes.append((c_idx, e_idx))

        for idx in removed_indexes:
            index_changes.append(f"    * Remove index: {idx[0]} ({idx[1]})")

        for idx in added_indexes:
            index_changes.append(f"    * Add index: {idx[0]} ({idx[1]})")

        for c_idx, e_idx in changed_indexes:
            index_changes.append(f"    * Change index: {c_idx[0]} -> {e_idx[1]}")

        return index_changes

    def create_schema_update_map(
        self,
        table_name,
        current_primary_key,
        expected_primary_key,
        current_order_by_key,
        expected_order_by_key,
        current_sample_by,
        expected_sample_by,
        current_indexes,
        expected_indexes,
    ):
        """
        Create a map of schema updates for a specific table.
        """
        self.schema_update_map[table_name] = {
            "current_primary_key": current_primary_key,
            "expected_primary_key": expected_primary_key,
            "current_order_by_key": current_order_by_key,
            "expected_order_by_key": expected_order_by_key,
            "current_sample_by": current_sample_by,
            "expected_sample_by": expected_sample_by,
            "current_indexes": current_indexes,
            "expected_indexes": expected_indexes,
        }
        self._log_schema_update_map()

    def _log_schema_update_map(self) -> None:
        """
        Log the schema update map in a more readable format.
        """
        report = []
        for table_name, details in self.schema_update_map.items():
            report.append(f"Table Diff: {table_name}")
            report.append("=" * 40)

            if details["current_primary_key"] != details["expected_primary_key"]:
                report.append(
                    f"--Current Primary Key: {details['current_primary_key']}"
                )
                report.append(
                    f"++Expected Primary Key: {details['expected_primary_key']} \n"
                )
            else:
                report.append(
                    f"Primary Key: {details['current_primary_key']} (unchanged)\n"
                )

            if details["current_order_by_key"] != details["expected_order_by_key"]:
                report.append(
                    f"--Current Order By Key: {details['current_order_by_key']}"
                )
                report.append(
                    f"++Expected Order By Key: {details['expected_order_by_key']} \n"
                )
            else:
                report.append(
                    f"Order By Key: {details['current_order_by_key']} (unchanged)\n"
                )

            if "current_sample_by" in details and "expected_sample_by" in details:
                if details["current_sample_by"] != details["expected_sample_by"]:
                    current_sample_display = (
                        '""'
                        if details["current_sample_by"] == ""
                        else details["current_sample_by"]
                    )
                    expected_sample_display = (
                        '""'
                        if details["expected_sample_by"] == ""
                        else details["expected_sample_by"]
                    )
                    report.append(f"--Current Sample By: {current_sample_display}")
                    report.append(f"++Expected Sample By: {expected_sample_display} \n")
                elif details["current_sample_by"]:
                    report.append(
                        f"Sample By: {details['current_sample_by']} (unchanged)\n"
                    )

            current_indexes = details["current_indexes"]
            expected_indexes = details["expected_indexes"]

            if current_indexes != expected_indexes:
                report.append("--Current Indexes:")
                for index in current_indexes:
                    report.append(f"  - Index Name: {index[0]}")
                    report.append(f"    Definition: {index[1]}")

                report.append("\n++Expected Indexes:")
                for index in expected_indexes:
                    report.append(f"  - Index Name: {index[0]}")
                    report.append(f"    Definition: {index[1]}")
            else:
                report.append("Indexes (unchanged):")
                for index in current_indexes:
                    report.append(f"  - Index Name: {index[0]}")
                    report.append(f"    Definition: {index[1]}")

            report.append("\n")

        report_output = "\n".join(report)
        logger.info(report_output)

    def capture_current_table_size(self, database_name, table_name):
        """
        Capture the current size of the table, total rows, and rows by day.
        """
        query_size = f"""
            SELECT database, table, active, sum(rows) total_rows, sum(bytes_on_disk) total_bytes_on_disk
            FROM clusterAllReplicas(default, system.parts)
            WHERE database='{database_name}' AND table='{table_name}' AND active=1
            GROUP BY database, table, active
            ORDER BY total_bytes_on_disk;
        """
        result_size = self.execute_query(query_size)

        query_total_rows = f"""
            SELECT count() AS total_rows
            FROM {database_name}.{table_name}
        """
        result_total_rows = self.execute_query(query_total_rows)

        query_rows_by_day = f"""
            SELECT toStartOfDay(timestamp_load) AS ts, count() AS daily_rows
            FROM {database_name}.{table_name}
            GROUP BY ts
            ORDER BY ts DESC
        """
        result_rows_by_day = self.execute_query(query_rows_by_day)

        size = result_size[0] if result_size else (None, 0, 0)
        total_size = size[4] if len(size) > 4 else 0
        total_rows = result_total_rows[0][0] if result_total_rows else 0
        rows_by_day = result_rows_by_day

        return TableStats(
            table_name=table_name,
            size_bytes=total_size,
            total_rows=total_rows,
            rows_by_day=rows_by_day,
        )

    def validate_parts_creation(self, database_name: str, table_name: str):
        """
        Validate that new parts have been created in the ClickHouse table.
        """
        query = f"""
            SELECT
            toStartOfMinute(event_time) ts,
            database,
            table,
            sum(rows) as total_rows,
            count()
            FROM clusterAllReplicas(default, system.part_log)
            WHERE event_type = 'NewPart' and event_date >= today() - interval 40 MINUTE and table = '{table_name}'
            AND database = '{database_name}'
            GROUP BY ts, database, table
            ORDER BY ts DESC
        """
        result = self.execute_query(query)
        return result

    def get_existing_schema_ddl(self, database_name: str, table_name: str):
        """
        Get the existing schema DDL for the table.
        """
        try:
            query = f"SHOW CREATE TABLE {database_name}.{table_name}"
            logger.warning(f"Showing Table {database_name}.{table_name}\n")

            result = self.ch_client.execute(query)
            if result:
                return result[0][0]
            else:
                logger.error(f"Table: {table_name} do not exists.")
                return None
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            if parsed_error['type'] == 'database_not_found':
                logger.warning(f"Database {database_name} does not exist.")
            elif parsed_error['type'] == 'table_not_found':
                logger.warning(
                    f"Table {table_name} in database {database_name} does not exist."
                )
            else:
                logger.warning(f"Error getting schema DDL for table {table_name}: {parsed_error['user_message']}")
            return None

    def check_table_records(self, database_name: str, table_name: str):
        """
        Check the number of records in the specified table.
        """
        query = f"SELECT COUNT(*) FROM {database_name}.{table_name}"
        result = self.execute_query(query)
        return result[0][0] if result else 0

    def execute_query(self, query: str):
        """
        Execute a query on the ClickHouse database and return the result.
        """
        try:
            result = self.ch_client.execute(query)
            return result
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Query execution failed: {parsed_error['user_message']} (Details: {parsed_error['message']})")
            return None

    def _normalize_data_type(self, data_type: str) -> str:
        """
        Normalize data type names to handle equivalent types and spacing differences.

        Args:
            data_type (str): The data type to normalize

        Returns:
            str: The normalized data type name
        """
        import re

        type_equivalents = {
            'Bool': 'Boolean',
            'Boolean': 'Boolean'
        }

        normalized = re.sub(r'\s*,\s*', ',', data_type)
        normalized = re.sub(r'\(\s*', '(', normalized)
        normalized = re.sub(r'\s*\)', ')', normalized)

        return type_equivalents.get(normalized, normalized)

    def _get_columns_with_types_from_ddl(self, sql_command: str) -> dict:
        """
        Extract columns and their data types from DDL statement.
        
        Args:
            sql_command (str): DDL statement
            
        Returns:
            dict: Dictionary mapping column names to their data types
        """
        import re
        columns = {}
        ddl_part = re.search(r'\((.*?)\)\s*ENGINE', sql_command, re.DOTALL)
        if ddl_part:
            for line in ddl_part.group(1).splitlines():
                line = line.strip()
                if line and not line.startswith('--') and not line.startswith('INDEX') and not line.startswith('PROJECTION'):
                    column_def = line.split()
                    if len(column_def) >= 2:
                        column_name = column_def[0].strip('`')
                        column_type = column_def[1].replace(' ', '').rstrip(',')
                        columns[column_name] = column_type
        return columns

    def _get_existing_columns_with_types(self, database_name: str, table_name: str) -> dict:
        """
        Get existing columns and their data types from the database.
        
        Args:
            database_name (str): Name of the database
            table_name (str): Name of the table
            
        Returns:
            dict: Dictionary mapping column names to their data types
        """
        query = f"""
            SELECT name, type 
            FROM system.columns 
            WHERE database = '{database_name}' AND table = '{table_name}'
        """
        result = self.execute_query(query)
        columns = {}
        if result:
            for name, column_type in result:
                columns[name] = column_type
        return columns

    def _detect_data_type_changes(self, database_name: str, table_name: str, expected_schema_ddl: str) -> list:
        """
        Detect data type changes between existing and expected schema.
        
        Args:
            database_name (str): Name of the database
            table_name (str): Name of the table
            expected_schema_ddl (str): Expected DDL statement
            
        Returns:
            list: List of data type changes with before/after information
        """
        data_type_changes = []
        
        try:
            expected_columns = self._get_columns_with_types_from_ddl(expected_schema_ddl)
            existing_columns = self._get_existing_columns_with_types(database_name, table_name)
            
            for column, new_data_type in expected_columns.items():
                if column in existing_columns:
                    existing_type = self._normalize_data_type(existing_columns[column])
                    new_type = self._normalize_data_type(new_data_type)
                    
                    if existing_type != new_type:
                        data_type_changes.append({
                            "column": column,
                            "current_type": existing_columns[column],
                            "expected_type": new_data_type
                        })
                        
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error detecting data type changes: {parsed_error['user_message']} (Details: {parsed_error['message']})")
            
        return data_type_changes
