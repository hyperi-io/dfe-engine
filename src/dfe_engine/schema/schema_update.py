import re
import os
import time
from pathlib import Path
from typing import List, Tuple, Dict, Optional
import uuid

from hs_lib.logger import logger
from ..clickhouse.clickhouse_manager import ClickHouseManager
from ..clickhouse.clickhouse_errors_mapping import ClickHouseErrorHandler
from ..schema.schema_util import SchemaUtils

class SchemaModifier:
    PARTITION_BY_STATEMENT = 'PARTITION BY toYYYYMMDD(timestamp_load)'
    TTL_STATEMENT = 'TTL timestamp + INTERVAL {ttl} DAY DELETE WHERE timestamp >= 0,'
    TTL_STATEMENT_LOAD = 'timestamp_load + INTERVAL {ttl} DAY DELETE WHERE timestamp_load >= 0'
    PROJECTION_BY_TIMESTAMP = """PROJECTION timestamp_optimized ( SELECT * ORDER BY timestamp )"""
    TABLE_SETTINGS = """SETTINGS\n    index_granularity = 2048,\n    ttl_only_drop_parts = 1;\n"""
    ENGINE_REPLICATED = 'ENGINE = ReplicatedMergeTree() '
    ENGINE_SHARED = 'ENGINE = SharedMergeTree() '
    ENGINE_MERGE = 'ENGINE = MergeTree()'
    
    def __init__(self, 
                 schema_update_flag: str,
                 drop_replacement_table_flag: str,
                 use_replicated_merge_tree: bool,
                 use_json_feature: bool,
                 use_subsampling_feature: bool,
                 use_shared_merge_tree: bool,
                 do_add_columns: bool,
                 dfe_output_directory: str,
                 organisations: list, 
                 max_insert_threads = int,
                 min_insert_block_size_rows = int,
                 schema_filter_list: str = None,
                 derived_schema_filter_list: str = None,
                 schema_filter_wildchar: str = None,
                 derived_schema_filter_wildchar: str = None,
                 target_config_data: dict = None,
                 logger = None 
            ):
        """
        Initialize the SchemaPlan with required parameters.
        
        :param schema_update_flag: Flag indicating whether schema updates are enabled ('YES' or 'NO').
        :param use_replicated_merge_tree: Flag indicating whether to use ReplicatedMergeTree engine.
        :param use_shared_merge_tree: Flag indicating whether to use SharedMergeTree engine.
        :param dfe_output_directory: Path to the directory containing schema output files.
        :param organisations: List of organisation dictionaries to process.
        :param max_insert_threads: Maximum number of threads to use for data insert operations.
        :param min_insert_block_size_rows: Minimum number of rows for data insert block size.
        :param schema_filter_list: (Optional) Comma-separated list of schema filters to apply.
        :param target_config_data: (Optional) Configuration data for connecting to ClickHouse.
        :param logger: (Optional) Logger instance for logging messages. If None, a default logger will be created.
        :param use_json_feature (bool) : Use JSON Feature 
        :param do_add_columns (bool) : Flag indicating whether to add new columns to the schema.
        :param use_subsampling_feature (bool) : Flag indicating whether to use subsampling feature.
        :param max_insert_threads (int): Maximum number of threads for insert operations.
        :param min_insert_block_size_rows (int): Minimum block size for insert operations.
        """
        self.dfe_output_directory = dfe_output_directory
        self.organisations = organisations
        self.schema_filter_list = schema_filter_list
        self.derived_schema_filter_list = derived_schema_filter_list
        self.schema_filter_wildchar = schema_filter_wildchar
        self.derived_schema_filter_wildchar = derived_schema_filter_wildchar
        self.schema_update_flag = schema_update_flag
        self.do_add_columns = do_add_columns
        self.drop_replacement_table_flag = drop_replacement_table_flag
        self.use_replicated_merge_tree = use_replicated_merge_tree
        self.use_json_feature = use_json_feature
        self.use_subsampling_feature = use_subsampling_feature
        self.use_shared_merge_tree = use_shared_merge_tree
        self.max_insert_threads = max_insert_threads
        self.min_insert_block_size_rows = min_insert_block_size_rows
        self.schema_update_map = {}
        self.target_config_data = target_config_data
        self.clickhouse_manager = ClickHouseManager.get_instance(target_config_data=target_config_data)
        self.ch_client = self.clickhouse_manager.get_clickhouse_client() 
        self.engine_statement = self.ENGINE_SHARED if self.use_shared_merge_tree else (self.ENGINE_REPLICATED if self.use_replicated_merge_tree else self.ENGINE_MERGE)
        
    def process_sql_scripts(self, is_api_call: bool = False) -> List[Dict]:
        """
        Processes and executes both regular SQL and view SQL scripts based on the provided customer data.

        This method walks through the directory containing SQL files, identifies regular and view SQL files, 
        and executes them accordingly. Regular SQL files are executed directly, while view SQL files are 
        processed to ensure they are created if not already existing. Finally, it logs the completion 
        of SQL script execution.
        
        It excludes specific files like 'create_databases.sql', 'create_roles.sql', 'create_service_accounts.sql'.
        
        Parameters:
            is_api_call (bool, optional): If True, return results as structured data for API use. Defaults to False.
            
        Returns:
            List[Dict]: If is_api_call is True, returns a list of structured results. Otherwise returns None.
        """
        if self.schema_update_flag == 'YES': 
            try:
                customer_data = SchemaUtils.read_customer_list(organisations=self.organisations, logger=logger)
                schema_files = self.collect_schema_files(customer_data)
                return self.execute_schema_files(schema_files, is_api_call=is_api_call)
            except Exception as e:
                parsed_error = ClickHouseErrorHandler.parse_error(e)
                logger.error(f"An error occurred while processing the SQL scripts: {parsed_error['user_message']}", exc_info=True)
                if is_api_call:
                    return []
                return None
        else: 
            logger.warning("Schema Update Flag is set to 'NO'. The schema update process will not proceed.")
            if is_api_call:
                return []
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
            for root, _, files in SchemaUtils.walk_schema_directory(dfe_output_directory=self.dfe_output_directory):
                schema_files.extend(SchemaUtils.filter_schema_files(org_id, root, files, self.schema_filter_list, self.derived_schema_filter_list, self.schema_filter_wildchar, self.derived_schema_filter_wildchar))

        if not schema_files:
            error_message = "No schemas found"
            raise Exception(error_message)
        
        logger.info(f"Using the following list of schemas: '{schema_files}'.")
        return schema_files

    def execute_schema_files(self, schema_files: List[Tuple[str, str, str]], is_api_call: bool = False) -> List[Dict]:
        """
        Execute the collected schema files.
        
        Parameters:
            schema_files (List[Tuple[str, str, str]]): List of tuples containing (org_id, root, file_name).
            is_api_call (bool, optional): If True, return results as structured data for API use. Defaults to False.
            
        Returns:
            List[Dict]: If is_api_call is True, returns a list of structured results. Otherwise returns None.
        """
        results = [] if is_api_call else None
        
        for org_id, root, file_name in schema_files:
            sql_file_path = os.path.join(root, file_name)
            table_name = Path(file_name).stem
            database_name = org_id
            with open(sql_file_path, 'r', encoding='utf-8') as ddl_file:
                ddl_content = ddl_file.read()
                if SchemaUtils.is_view(ddl_content):
                    logger.warning(f"This is a View {table_name} so it will be skipped from processing")
                    continue
                logger.info("\n\n*** Executing SQL file ...%s", file_name)
                try:
                    result = self.modify_schemas(ddl_content, database_name, table_name, is_api_call=is_api_call)
                    if is_api_call and result:
                        results.append(result)
                except Exception as e:
                    parsed_error = ClickHouseErrorHandler.parse_error(e)
                    logger.error(f"Error processing schema file {file_name}: {parsed_error['user_message']}", exc_info=True)
                    if is_api_call:
                        results.append({
                            "schema_name": table_name,
                            "organisation_id": database_name,
                            "database_name": database_name,
                            "table_name": table_name,
                            "status": "error",
                            "error_message": parsed_error['user_message'],
                            "error_details": parsed_error['message'],
                            "error_code": parsed_error['code'],
                            "error_type": parsed_error['type']
                        })
                    else:
                        exit(0)
                        
        return results

    def modify_schemas(self, ddl_statement: str, database_name: str, table_name: str, is_api_call: bool = False) -> Optional[Dict]:
        """
        Modify the schema of a specified table by creating a replacement table, 
        exchanging it with the original table, and ensuring data consistency.

        This method performs the following steps:

        0. **Validate Data Types**:
            - Added validation for detecting changes in data types for tables. Any discrepancies will trigger a error, requiring manual intervention for handling the updates.
        
        1. **Add Columns**:
            - Checks if any new columns to be added to the schema.. 
        
        2. **Update Primary Key or Order By **:
            - Checks if any updates to the schema's  primary key, or order by key.
        
        3. **Create Replacement Table**:
            - If schema differences are detected and schema updates are enabled, 
            create a replacement table with the updated schema, including the primary key, 
            order by key, SAMPLE BY clause, and TTL values.

        4. **Exchange Tables**:
            - Swap the original table with the newly created replacement table.

        5. **Check Table Records**:
            - Verify that both the replacement and original tables contain records after the exchange.
            - Log a warning if either table is found to be empty.

        6. **Insert Records**:
            - Insert records into the new replacement table.

        7. **Poll System Processes**:
            - Ensure insertion is complete before validating data migration was successful thorugh polling 'system.processes'.
        
        8. **Validate Data Migration**:
            - Ensure that the data has been accurately migrated from the original to the replacement table.
            
        Parameters:
            ddl_statement (str): The DDL statement to modify.
            database_name (str): The database name to modify.
            table_name (str): The table name to modify.
            is_api_call (bool, optional): If True, return the results as structured data instead of just logging.
                                          For FastAPI endpoint use. Defaults to False.
        
        Returns:
            dict: If is_api_call is True, returns a structured dict with update results.
                  Otherwise returns None.
        """
        logger.info(f"Processing table: {database_name}.{table_name}")
        
        api_result = None
        if is_api_call:
            api_result = {
                "schema_name": table_name,
                "organisation_id": database_name,
                "database_name": database_name,
                "table_name": table_name,
                "status": "processing",
                "changes": {},
                "columns_added": [],
                "indexes_modified": False,
                "error_message": None,
                "warnings": [],
                "current_primary_key": None,
                "expected_primary_key": None,
                "current_order_by_key": None,
                "expected_order_by_key": None,
                "current_sample_by": None,
                "expected_sample_by": None,
                "current_indexes": [],
                "expected_indexes": [],
                "table_size_bytes": 0,
                "total_rows": 0,
                "has_changes": False,
                "change_summary": "",
                "columns_to_add": [],
                "data_type_mismatches": []
            }
 
        try: 
            current_schema_ddl = self.get_existing_schema_ddl(database_name, table_name)
            if isinstance(current_schema_ddl, dict) and current_schema_ddl.get('error'):
                logger.warning(f"Could not retrieve current schema DDL for {database_name}.{table_name}: {current_schema_ddl['error_message']}")
                if is_api_call:
                    api_result["status"] = "error"
                    api_result["error_message"] = current_schema_ddl['error_message']
                    api_result["error_type"] = current_schema_ddl['error_type']
                    api_result["error_details"] = current_schema_ddl.get('error_details')
                    api_result["error_code"] = current_schema_ddl.get('error_code')
                    return api_result
                return None
            
            expected_schema_ddl = ddl_statement
            new_columns = self.parse_columns_from_ddl(expected_schema_ddl)
            existing_columns = self.fetch_existing_columns(database_name, table_name)
            add_column_schema_differences = self.generate_alter_statements(existing_columns, new_columns, database_name, table_name, expected_schema_ddl)    
            
            other_schema_differences, expected_primary_key, expected_order_by_key, current_indexes, expected_indexes, ttl_value, expected_sample_by, current_primary_key, current_order_by_key, current_sample_by = \
                self.detect_schema_differences(database_name, table_name, current_schema_ddl, expected_schema_ddl)
            
            columns_to_add = list(set(new_columns) - set(existing_columns))
            
            self.print_schema_update_report(
                database_name=database_name,
                table_name=table_name,
                current_primary_key=current_primary_key,
                expected_primary_key=expected_primary_key,
                current_order_by_key=current_order_by_key,
                expected_order_by_key=expected_order_by_key,
                current_sample_by=current_sample_by,
                expected_sample_by=expected_sample_by,
                current_indexes=current_indexes,
                expected_indexes=expected_indexes,
                columns_to_add=columns_to_add
            )
            
            has_changes = (
                current_primary_key != expected_primary_key or
                current_order_by_key != expected_order_by_key or
                current_sample_by != expected_sample_by or
                bool(columns_to_add) or
                current_indexes != expected_indexes
            )
            
            if is_api_call:
                try:
                    table_size_query = f"SELECT sum(bytes) as size FROM system.parts WHERE database = '{database_name}' AND table = '{table_name}'"
                    size_result = self.ch_client.execute(table_size_query)
                    table_size_bytes = size_result[0][0] if size_result and size_result[0][0] else 0
                    
                    row_count_query = f"SELECT count() FROM {database_name}.{table_name}"
                    count_result = self.ch_client.execute(row_count_query)
                    total_rows = count_result[0][0] if count_result and count_result[0][0] else 0
                except Exception as stats_error:
                    parsed_error = ClickHouseErrorHandler.parse_error(stats_error)
                    logger.warning(f"Could not get table statistics for {database_name}.{table_name}: {parsed_error['user_message']}")
                    table_size_bytes = 0
                    total_rows = 0
                
                api_result["table_size_bytes"] = table_size_bytes
                api_result["total_rows"] = total_rows
                api_result["current_primary_key"] = current_primary_key
                api_result["expected_primary_key"] = expected_primary_key
                api_result["current_order_by_key"] = current_order_by_key
                api_result["expected_order_by_key"] = expected_order_by_key
                api_result["current_sample_by"] = current_sample_by
                api_result["expected_sample_by"] = expected_sample_by
                api_result["current_indexes"] = [{"name": idx[0], "definition": idx[1]} for idx in current_indexes]
                api_result["expected_indexes"] = [{"name": idx[0], "definition": idx[1]} for idx in expected_indexes]
                api_result["columns_to_add"] = columns_to_add
                
                def normalize_string_for_comparison(value):
                    """Normalize string values for robust comparison"""
                    if value is None:
                        return ""
                    return str(value).strip()
                
                def normalize_indexes(indexes):
                    """Normalize indexes for comparison by sorting and cleaning definitions."""
                    normalized = []
                    for idx_name, idx_def in indexes:
                        clean_def = idx_def.strip().rstrip(',').strip()
                        normalized.append((idx_name, clean_def))
                    return sorted(normalized, key=lambda x: x[0])
                
                normalized_current_pk = normalize_string_for_comparison(current_primary_key)
                normalized_expected_pk = normalize_string_for_comparison(expected_primary_key)
                normalized_current_ok = normalize_string_for_comparison(current_order_by_key)
                normalized_expected_ok = normalize_string_for_comparison(expected_order_by_key)
                normalized_current_sb = normalize_string_for_comparison(current_sample_by)
                normalized_expected_sb = normalize_string_for_comparison(expected_sample_by)
                
                normalized_current_indexes = normalize_indexes(current_indexes)
                normalized_expected_indexes = normalize_indexes(expected_indexes)
                indexes_actually_differ = normalized_current_indexes != normalized_expected_indexes
                
                api_has_changes = (
                    normalized_current_pk != normalized_expected_pk or
                    normalized_current_ok != normalized_expected_ok or
                    normalized_current_sb != normalized_expected_sb or
                    bool(columns_to_add) or
                    indexes_actually_differ
                )
                api_result["has_changes"] = api_has_changes
                
                change_parts = []
                if normalized_current_pk != normalized_expected_pk:
                    change_parts.append("Primary key changes")
                if normalized_current_ok != normalized_expected_ok:
                    change_parts.append("Order by key changes")
                if normalized_current_sb != normalized_expected_sb:
                    change_parts.append("Sample by changes")
                if columns_to_add:
                    change_parts.append(f"{len(columns_to_add)} columns to add")
                if indexes_actually_differ:
                    change_parts.append("Index changes")
                
                api_result["change_summary"] = ", ".join(change_parts) if change_parts else "No changes detected"
                
                api_result["changes"] = {
                    "key_structure": {
                        "primary_key": {
                            "current": current_primary_key,
                            "expected": expected_primary_key,
                            "modified": normalized_current_pk != normalized_expected_pk
                        },
                        "order_by_key": {
                            "current": current_order_by_key,
                            "expected": expected_order_by_key,
                            "modified": normalized_current_ok != normalized_expected_ok
                        },
                        "sample_by": {
                            "current": current_sample_by,
                            "expected": expected_sample_by,
                            "modified": normalized_current_sb != normalized_expected_sb
                        }
                    },
                    "indexes": {
                        "current": [{"name": idx[0], "definition": idx[1]} for idx in current_indexes],
                        "expected": [{"name": idx[0], "definition": idx[1]} for idx in expected_indexes],
                        "modified": indexes_actually_differ
                    },
                    "columns": {
                        "to_add": columns_to_add
                    }
                }
                
                other_schema_differences = (
                    current_primary_key != expected_primary_key or
                    current_order_by_key != expected_order_by_key or
                    current_sample_by != expected_sample_by or
                    indexes_actually_differ
                )
            
            try:
                logger.info(f"\nStep 0: Validating of datatype changes for table '{table_name}' to ensure consistency and correctness...")
                columns_with_new_datatypes = self.get_columns_with_types(expected_schema_ddl)
                columns_with_existing_datatypes = self.get_existing_columns_with_types(database_name, table_name)
                self.compare_columns_and_data_types(columns_with_existing_datatypes, columns_with_new_datatypes, database_name, table_name)	
            except Exception as e:
                parsed_error = ClickHouseErrorHandler.parse_error(e)
                error_msg = f"Error in Step 0: Validating of datatype changes for table {table_name}: {parsed_error['user_message']}"
                logger.error(error_msg)
                if is_api_call:
                    if api_result is not None:
                        api_result["status"] = "error"
                        api_result["error_message"] = error_msg
                        api_result["error_details"] = parsed_error['message']
                        api_result["error_code"] = parsed_error['code']
                        api_result["error_type"] = parsed_error['type']
                        return api_result
                raise Exception(error_msg) from e
            
            if self.do_add_columns and len(add_column_schema_differences) > 0:
                for alter_sql in add_column_schema_differences:
                    logger.info("\nStep 2: Schema column differences detected. New Columns need to be added.")
                    logger.info(f"Executing ALTER statement: {alter_sql}")                   
                    if self.use_json_feature:
                        set_json_type_command = "SET allow_experimental_json_type = 1;"
                        self.ch_client.execute(set_json_type_command)
                        logger.debug(f"Executed: {set_json_type_command}")
                    
                    self.ch_client.execute(alter_sql)
                    logger.info(f"New Columns for Schema {database_name}.{table_name} added successfully.")
                    
                    if is_api_call:
                        api_result["columns_added"].extend(columns_to_add)
            else:
                logger.warning(
                    f"Step 2: No column differences found for schema {database_name}.{table_name} or make sure do_add_columns parameter is set to True in DFE Package Config."
                )

            try:
                index_changes = self.compare_indexes(current_indexes, expected_indexes)
                index_modified = bool(index_changes['added'] or index_changes['removed'] or index_changes['modified'])
                
                if index_modified:
                    logger.info(f"\nStep 2: Updating indexes on the table '{table_name}' based on expected schema...")
                    
                    if index_changes['added']:
                        logger.info(f"  Indexes to be added: {[idx[0] for idx in index_changes['added']]}")
                    if index_changes['removed']:
                        logger.info(f"  Indexes to be removed: {[idx[0] for idx in index_changes['removed']]}")
                    if index_changes['modified']:
                        logger.info(f"  Indexes to be modified: {[idx[0][0] for idx in index_changes['modified']]}")
                    
                    if is_api_call:
                        api_result["indexes_modified"] = True
                        api_result["changes"]["indexes"]["modified"] = True
                        api_result["changes"]["indexes"]["added"] = [{"name": idx[0], "definition": idx[1]} for idx in index_changes['added']]
                        api_result["changes"]["indexes"]["removed"] = [{"name": idx[0], "definition": idx[1]} for idx in index_changes['removed']]
                        api_result["changes"]["indexes"]["modified"] = [{"name": old[0][0], "old_definition": old[0][1], "new_definition": new[1]} 
                                                                       for old, new in index_changes['modified']]
                    
                    self.add_indexes(database_name, table_name, current_indexes, expected_indexes)
                    self.remove_indexes(database_name, table_name, current_indexes, expected_indexes)
                else:
                    logger.info(f"\nNo index changes required for table '{table_name}'.")
            except Exception as e:
                parsed_error = ClickHouseErrorHandler.parse_error(e)
                error_msg = f"Error in Step 2: Updating indexes: {parsed_error['user_message']}"
                logger.error(error_msg)
                if is_api_call and api_result is not None:
                    api_result["status"] = "error"
                    api_result["error_message"] = error_msg
                    return api_result
                raise Exception(f"Error in Step 2: Updating indexes: {parsed_error['user_message']}") from e
                
            if other_schema_differences:
                logger.info(f"Other Schema differences like Indexes or Primary Key or Order By Key detected for table '{table_name}' in database '{database_name}'.")
                if is_api_call:
                    api_result["changes"]["schema_structure_modified"] = True
                    
                if self.drop_replacement_table_flag == 'NO':
                    logger.warning(f"Rebuild table flag is set to False. Skipping table rebuild operations for {database_name}.{table_name}")
                    logger.warning(f"Schema differences were detected but table will not be rebuilt. Only index changes were applied. If you want to rebuild table then set drop_replacement_table_flag to YES")
                    if is_api_call:
                        api_result["status"] = "partial_success"
                        api_result["changes"]["rebuild_skipped"] = True
                        return api_result
                    exit(0)
                else:
                    if self.check_nullable_primary_key(database_name, table_name, expected_primary_key):
                        error_msg = f"Table '{database_name}.{table_name}' contains nullable columns in primary key. Skipping all updates for this table."
                        logger.error(error_msg)
                        if is_api_call and api_result is not None:
                            api_result["status"] = "error"
                            api_result["error_message"] = error_msg
                            return api_result
                        raise Exception(error_msg)
                    else: 
                        try:
                            logger.info(f"\nStep 1: Creating replacement table 'replacement_{table_name}' with the expected primary key, order by key, and sample by clause...")
                            self.create_replacement_table(database_name, table_name, expected_primary_key, expected_order_by_key, expected_sample_by, ttl_value)
                            if is_api_call:
                                api_result["changes"]["replacement_table_created"] = True
                        except Exception as e:
                            parsed_error = ClickHouseErrorHandler.parse_error(e)
                            error_msg = f"Error in Step 1: Creating replacement table: {parsed_error['user_message']}"
                            logger.error(error_msg)
                            if is_api_call:
                                api_result["status"] = "error"
                                api_result["error_message"] = error_msg
                                return api_result
                            raise Exception(error_msg) from e

                        try:
                            logger.info(f"\nStep 2: Exchanging original table '{table_name}' with replacement table 'replacement_{table_name}'...")
                            initial_original_count = self.check_table_records(database_name, table_name)
                            if initial_original_count == 0:
                                logger.warning(f"Warning: Table ('{table_name}') has no records before the exchange.")
                                if is_api_call:
                                    api_result["changes"]["empty_table_before_exchange"] = True
                            self.exchange_tables(database_name, table_name)
                            if is_api_call:
                                api_result["changes"]["tables_exchanged"] = True
                        except Exception as e:
                            parsed_error = ClickHouseErrorHandler.parse_error(e)
                            error_msg = f"Error in Step 2: Exchanging tables: {parsed_error['user_message']}"
                            logger.error(error_msg)
                            if is_api_call:
                                api_result["status"] = "error"
                                api_result["error_message"] = error_msg
                                return api_result
                            raise Exception(error_msg) from e

                        try:
                            logger.info("\nStep 3: Checking record counts for original and replacement tables after exchange...")
                            initial_replacement_count = self.check_table_records(database_name, f"replacement_{table_name}")
                            if initial_replacement_count == 0:
                                logger.warning(f"Warning: Replacement Table ('replacement_{table_name}') has no records after the exchange.")
                                if is_api_call:
                                    api_result["changes"]["empty_replacement_table_after_exchange"] = True
                        except Exception as e:
                            parsed_error = ClickHouseErrorHandler.parse_error(e)
                            error_msg = f"Error in Step 3: Checking table records: {parsed_error['user_message']}"
                            logger.error(error_msg)
                            if is_api_call:
                                api_result["status"] = "error"
                                api_result["error_message"] = error_msg
                                return api_result
                            raise Exception(error_msg) from e

                        migration_query_id = str(uuid.uuid4())
                        try:
                            logger.info(f"\nStep 5: Inserting records from 'replacement_{table_name}' back into the original table '{table_name}'...")
                            self.insert_records_into_new_table(database_name, table_name, query_id=migration_query_id)
                            if is_api_call:
                                api_result["changes"]["records_inserted"] = True
                        except Exception as e:
                            parsed_error = ClickHouseErrorHandler.parse_error(e)
                            error_msg = f"Error in Step 5: Inserting records: {parsed_error['user_message']}"
                            logger.error(error_msg)
                            if is_api_call:
                                api_result["status"] = "error"
                                api_result["error_message"] = error_msg
                                return api_result
                            raise Exception(error_msg) from e

                        try:
                            logger.info(f"\nStep 6: Beginning polling of 'system.processes' to ensure '{table_name}' insertion is complete before validation...")
                            self.poll_data_migration_job(migration_query_id, polling_interval = 20)
                            if is_api_call:
                                api_result["changes"]["data_migration_completed"] = True
                        except Exception as e:
                            parsed_error = ClickHouseErrorHandler.parse_error(e)
                            error_msg = f"Error in Step 6: Poll System Processes: {parsed_error['user_message']}"
                            logger.error(error_msg)
                            if is_api_call:
                                api_result["status"] = "error"
                                api_result["error_message"] = error_msg
                                return api_result
                            raise Exception(error_msg) from e

                        try:
                            logger.info(f"\nStep 7: Validating data migration for table '{table_name}' to ensure consistency and correctness...")
                            self.validate_data_migration(database_name, table_name)
                            if is_api_call:
                                api_result["changes"]["data_validation_successful"] = True
                        except Exception as e:
                            parsed_error = ClickHouseErrorHandler.parse_error(e)
                            error_msg = f"Error in Step 7: Validating data migration: {parsed_error['user_message']}"
                            logger.error(error_msg)
                            if is_api_call:
                                api_result["status"] = "error"
                                api_result["error_message"] = error_msg
                                return api_result
                            raise Exception(error_msg) from e
                            
                        logger.info(f"\nRepartitioning process for table '{table_name}' completed successfully.")
                        if is_api_call:
                            api_result["status"] = "success"
                            if api_result.get("has_changes", False):
                                api_result["change_summary"] = api_result.get("change_summary", "Schema updated successfully")
                            else:
                                api_result["change_summary"] = "No changes were needed"
                            return api_result

            else:
                logger.warning(f"No Other Schema differences like Primary Key or Order By Key detected for schema: {database_name}.{table_name}")
                if is_api_call:
                    api_result["status"] = "no_other_changes"
                    return api_result

        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            error_msg = f"An error occurred in updating the schema: {parsed_error['user_message']}"
            logger.error(error_msg)
            if is_api_call and api_result is not None:
                api_result["status"] = "error"
                api_result["error_message"] = error_msg
                api_result["error_details"] = parsed_error['message']
                api_result["error_code"] = parsed_error['code']
                api_result["error_type"] = parsed_error['type']
                return api_result
            
        if is_api_call and api_result is not None:
            if api_result.get("status") == "processing":
                api_result["status"] = "success"
                api_result["change_summary"] = api_result.get("change_summary", "Schema processing completed")
            return api_result

    def print_schema_update_report(self, database_name, table_name, current_primary_key, expected_primary_key,
                                  current_order_by_key, expected_order_by_key, current_sample_by, expected_sample_by,
                                  current_indexes, expected_indexes, columns_to_add):
        """
        Print a well-structured report of schema updates.
        """
        report = []
        report.append("\n" + "="*80)
        report.append(f"SCHEMA UPDATE REPORT: {database_name}.{table_name}")
        report.append("="*80)
        
        report.append("\n[1] KEY STRUCTURE DIFFERENCES")
        report.append("-"*50)
        
        current_pk_display = current_primary_key if current_primary_key else "None"
        expected_pk_display = expected_primary_key if expected_primary_key else "None"
        current_ob_display = current_order_by_key if current_order_by_key else "None"
        expected_ob_display = expected_order_by_key if expected_order_by_key else "None"
        current_sb_display = f'""' if current_sample_by == "" else current_sample_by
        expected_sb_display = f'""' if expected_sample_by == "" else expected_sample_by
        
        key_differences = False
        
        if current_pk_display != expected_pk_display:
            report.append(f"PRIMARY KEY:")
            report.append(f"  - Current:  {current_pk_display}")
            report.append(f"  - Expected: {expected_pk_display}")
            report.append("")
            key_differences = True
        
        if current_ob_display != expected_ob_display:
            report.append(f"ORDER BY KEY:")
            report.append(f"  - Current:  {current_ob_display}")
            report.append(f"  - Expected: {expected_ob_display}")
            report.append("")
            key_differences = True
        
        if current_sb_display != expected_sb_display:
            report.append(f"SAMPLE BY:")
            report.append(f"  - Current:  {current_sb_display}")
            report.append(f"  - Expected: {expected_sb_display}")
            report.append("")
            key_differences = True
        
        if not key_differences:
            report.append("No key structure differences detected.")
            report.append("")
        
        report.append("\n[2] INDEX STRUCTURE DIFFERENCES")
        report.append("-"*50)
        
        removed_indexes = [idx for idx in current_indexes if idx[0] not in [e_idx[0] for e_idx in expected_indexes]]
        added_indexes = [idx for idx in expected_indexes if idx[0] not in [c_idx[0] for c_idx in current_indexes]]
        
        changed_indexes = []
        for c_idx in current_indexes:
            for e_idx in expected_indexes:
                if c_idx[0] == e_idx[0] and c_idx[1] != e_idx[1]:
                    changed_indexes.append((c_idx, e_idx))
        
        if not (removed_indexes or added_indexes or changed_indexes):
            report.append("No index changes detected.")
        else:
            if removed_indexes:
                report.append("INDEXES TO BE REMOVED:")
                for idx in removed_indexes:
                    report.append(f"  - {idx[0]}: {idx[1]}")
                report.append("")
            
            if added_indexes:
                report.append("INDEXES TO BE ADDED:")
                for idx in added_indexes:
                    report.append(f"  - {idx[0]}: {idx[1]}")
                report.append("")
            
            if changed_indexes:
                report.append("INDEXES TO BE MODIFIED:")
                for c_idx, e_idx in changed_indexes:
                    report.append(f"  - {c_idx[0]}:")
                    report.append(f"    Current:  {c_idx[1]}")
                    report.append(f"    Expected: {e_idx[1]}")
                report.append("")
        
        report.append("\n[3] COLUMN DIFFERENCES")
        report.append("-"*50)
        
        if columns_to_add:
            report.append("COLUMNS TO BE ADDED:")
            for column in columns_to_add:
                report.append(f"  - {column}")
        else:
            report.append("No column additions required.")
            
        report.append("\n" + "="*80 + "\n")
        
        logger.info("\n".join(report))
        
    def detect_schema_differences(self, database_name: str, table_name: str, current_schema_ddl: str, expected_schema_ddl: str):
        """
        Detect differences between the current table schema and the expected schema.

        :param table_name: The name of the table to check.
        :param current_schema_ddl: The current schema DDL statement to compare against.
        :param expected_schema_ddl: The expected schema DDL statement to compare against.
        :return: Tuple containing schema differences and extracted schema components.
        """
        current_primary_key = None
        current_order_by_key = None
        current_sample_by = ""
        current_indexes = []
        
        expected_primary_key, expected_order_by_key, expected_indexes, ttl_value, expected_sample_by, expected_partition_by, expected_projection, expected_table_settings = SchemaUtils.extract_keys_and_indexes_from_ddl(expected_schema_ddl)
        expected_sample_by = expected_sample_by or ""
        
        if not expected_order_by_key and expected_primary_key:
            expected_order_by_key = expected_primary_key
            logger.info(f"ORDER BY is empty for {database_name}.{table_name}, using PRIMARY KEY as fallback: {expected_order_by_key}")
        
        if not expected_order_by_key:
            logger.warning(f"Expected DDL for {database_name}.{table_name} does not contain ORDER BY clause!")

        current_schema_query = f"""
            SELECT primary_key, sorting_key
            FROM system.tables
            WHERE database = '{database_name}' AND name = '{table_name}';
        """
        current_schema = self.ch_client.execute(current_schema_query)

        if current_schema:
            current_primary_key = current_schema[0][0]
            current_order_by_key = current_schema[0][1]

        if current_schema_ddl:
            _, _, current_indexes, _, current_sample_by_from_ddl, current_partition_by, current_projection, current_table_settings = SchemaUtils.extract_keys_and_indexes_from_ddl(current_schema_ddl)
            current_sample_by = current_sample_by_from_ddl or ""
        
        schema_difference = (current_primary_key != expected_primary_key or
                           current_order_by_key != expected_order_by_key or
                           current_sample_by != expected_sample_by)

        return (
            schema_difference, 
            expected_primary_key, 
            expected_order_by_key, 
            current_indexes, 
            expected_indexes, 
            ttl_value, 
            expected_sample_by,
            current_primary_key, 
            current_order_by_key,
            current_sample_by    
        )

    def _construct_schema_builder_aligned_order_by(self, order_by: str, primary_key: str) -> str:
        """
        Construct ORDER BY following schema builder pattern.
        Schema builder ALWAYS starts with:
        1. cityHash64(timestamp_load) (if subsampling enabled)
        2. timestamp_load (always present)
        3. Then other columns
        
        Args:
            order_by: Original ORDER BY clause
            primary_key: Primary key columns
            
        Returns:
            ORDER BY clause aligned with schema builder pattern
        """
        final_columns = []
        
        
        if self.use_subsampling_feature:
            final_columns.append("cityHash64(timestamp_load)")
        final_columns.append("timestamp_load")
        
        
        additional_columns = []
        
        if order_by and order_by.strip():
            
            order_columns = [col.strip() for col in order_by.split(",")]
            for col in order_columns:
                clean_col = col.strip()
                if clean_col and clean_col != "timestamp_load" and clean_col != "cityHash64(timestamp_load)":
                    if clean_col not in additional_columns:
                        additional_columns.append(clean_col)
        
        
        if not additional_columns and primary_key and primary_key.strip():
            primary_columns = [col.strip() for col in primary_key.split(",")]
            for col in primary_columns:
                clean_col = col.strip()
                if clean_col and clean_col != "timestamp_load" and clean_col != "cityHash64(timestamp_load)":
                    if clean_col not in additional_columns:
                        additional_columns.append(clean_col)
        
        
        final_columns.extend(additional_columns)
        
        return ", ".join(final_columns)

    def create_replacement_table(self, database_name: str, table_name: str, primary_key: str, order_by: str, sample_by: str, ttl_value: str):
        """
        Create a replacement table by copying the structure of the existing table
        and applying the necessary ORDER BY, PRIMARY KEY, and TTL statements.
        """
        try:
            if self.table_exists(database_name, f"replacement_{table_name}"):
                logger.warning(f"Replacement table '{database_name}.replacement_{table_name}' already exists. Value of drop_replacement_table_flag is {self.drop_replacement_table_flag}")
                if self.drop_replacement_table_flag == 'YES':
                    logger.info(f"Dropping replacement table...")
                    self.drop_replacement_table(database_name, table_name)
                else:
                    raise Exception(f"Replacement table '{database_name}.replacement_{table_name}' already exists.")

            current_ddl = self.get_existing_schema_ddl(database_name, table_name)
            if isinstance(current_ddl, dict) and current_ddl.get('error'):
                raise Exception(f"Cannot retrieve DDL for table {database_name}.{table_name}: {current_ddl['error_message']}")
            elif current_ddl is None:
                raise Exception(f"Cannot retrieve DDL for table {database_name}.{table_name}. Unable to create replacement table.")
            
            _, _, _, _, current_sample_by, _, _, _ = SchemaUtils.extract_keys_and_indexes_from_ddl(current_ddl)

            ttl_statement = self.TTL_STATEMENT.replace('{ttl}', ttl_value)
            ttl_statement_load = self.TTL_STATEMENT_LOAD.replace('{ttl}', ttl_value)
            sample_clause = ""
            
            if current_sample_by and not sample_by:
                logger.warning(f"Current table has SAMPLE BY: '{current_sample_by}' but target schema does not.")
                logger.warning(f"ClickHouse requires SAMPLE BY to be present when it exists in the original table.")
                sample_by = current_sample_by
                sample_clause = f"SAMPLE BY {sample_by}"
                
                if 'cityHash64' in sample_by:
                    logger.warning(f"Adding sampling expression to both primary key and order by for '{database_name}.{table_name}'")
                    
                    if 'cityHash64' not in primary_key:
                        primary_key = f"{sample_by}, {primary_key}"
                    
                    if 'cityHash64' not in order_by:
                        order_by = f"{sample_by}, {order_by}"
            elif self.use_subsampling_feature and sample_by and sample_by.strip():
                sample_clause = f"SAMPLE BY {sample_by}"
                
                if 'cityHash64' in sample_by:
                    if 'cityHash64' not in primary_key:
                        logger.warning(f"Adding sampling expression to primary key for '{database_name}.{table_name}'")
                        primary_key = f"{sample_by}, {primary_key}"
                    
                    if 'cityHash64' not in order_by:
                        logger.warning(f"Adding sampling expression to order by key for '{database_name}.{table_name}'")
                        order_by = f"{sample_by}, {order_by}"
            
            logger.info(f"Creating replacement table for {database_name}.{table_name}")
            
            if not primary_key or primary_key.strip() == "":
                error_msg = f"Primary key is empty or None for table {database_name}.{table_name}. Cannot create replacement table with empty primary key."
                logger.error(error_msg)
                raise Exception(error_msg)
            
            original_order_by = order_by
            
            
            
            schema_builder_order_by = self._construct_schema_builder_aligned_order_by(order_by, primary_key)
            
            if schema_builder_order_by != order_by:
                if not order_by or order_by.strip() == "":
                    logger.warning(f"Empty ORDER BY detected - using schema builder pattern")
                else:
                    logger.warning(f"ORDER BY '{order_by}' does not follow schema builder pattern - aligning")
                order_by = schema_builder_order_by
                
            
            
            order_by_columns = [col.strip() for col in order_by.split(',')]
            primary_key_columns = [col.strip() for col in primary_key.split(',')]
            
            
            
            schema_builder_prefix = []
            if order_by_columns:
                schema_builder_prefix.append(order_by_columns[0])  
                if len(order_by_columns) > 1 and 'cityHash64' in order_by_columns[1]:
                    schema_builder_prefix.append(order_by_columns[1])  
            
            
            for col in primary_key_columns:
                if col != 'timestamp_load' and col in order_by_columns:
                    
                    col_index_in_order_by = order_by_columns.index(col)
                    if col_index_in_order_by >= len(schema_builder_prefix) and col not in schema_builder_prefix:
                        
                        for i in range(len(schema_builder_prefix), col_index_in_order_by + 1):
                            if order_by_columns[i] not in schema_builder_prefix:
                                schema_builder_prefix.append(order_by_columns[i])
                    
            schema_builder_primary_key = ', '.join(schema_builder_prefix)
            
            if schema_builder_primary_key != primary_key:
                logger.warning(f"PRIMARY KEY '{primary_key}' does not follow schema builder pattern - aligning as prefix of ORDER BY: '{schema_builder_primary_key}'")
                primary_key = schema_builder_primary_key
            
            if not order_by or order_by.strip() == "":
                logger.warning(f"ORDER BY is still empty after processing, using PRIMARY KEY as fallback")
                order_by = primary_key
            
            query_parts = [
                f"CREATE TABLE {database_name}.replacement_{table_name} AS {database_name}.{table_name}",
                self.engine_statement,
                self.PARTITION_BY_STATEMENT,
                f"PRIMARY KEY ({primary_key})",
                f"ORDER BY ({order_by})"
            ]
            
            if sample_clause and sample_clause.strip():
                query_parts.append(sample_clause)
            
            if ttl_statement and ttl_statement.strip():
                query_parts.append(ttl_statement)
                
            if ttl_statement_load and ttl_statement_load.strip():
                query_parts.append(ttl_statement_load)
                
            query_parts.append(self.TABLE_SETTINGS)
            
            query = "\n".join(query_parts)
            
            logger.info(f"Generated DDL for creating replacement table '{database_name}.replacement_{table_name}':\n{query.strip()}")
            self.execute_query(query)
            
            if not self.table_exists(database_name, f"replacement_{table_name}"):
                raise Exception(f"Replacement table '{database_name}.replacement_{table_name}' was not created successfully")
            
            logger.info(f"Successfully created replacement table '{database_name}.replacement_{table_name}'")

        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            error_msg = f"An error occurred in creating replacement table: {parsed_error['user_message']}"
            logger.error(error_msg)
            raise Exception(error_msg) from e

    def table_exists(self, database_name: str, table_name: str):
        """
        Check if a table exists in the specified database.
        
        Parameters:
            database_name (str): The name of the database.
            table_name (str): The name of the table to check.
        
        Returns:
            bool: True if the table exists, False otherwise.
        """
        try:
            query = f"SELECT COUNT(*) FROM system.tables WHERE database = '{database_name}' AND name = '{table_name}'"
            result = self.execute_query(query)
            exists = result[0][0] > 0
            logger.debug(f"Table exists check: {database_name}.{table_name} = {exists}")
            return exists
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error checking if table exists {database_name}.{table_name}: {parsed_error['user_message']}")
            return False   

    def compare_indexes(self, current_indexes: list, expected_indexes: list) -> dict:
        """
        Compare current and expected indexes and categorize the differences.
        
        Args:
            current_indexes: List of tuples containing (index_name, index_definition)
            expected_indexes: List of tuples containing (index_name, index_definition)
            
        Returns:
            dict: Dictionary with categories of index changes: 'added', 'removed', 'modified'
        """
        current_index_dict = {idx[0]: idx[1] for idx in current_indexes}
        expected_index_dict = {idx[0]: idx[1] for idx in expected_indexes}
        
        added_indexes = [idx for idx in expected_indexes if idx[0] not in current_index_dict]
        removed_indexes = [idx for idx in current_indexes if idx[0] not in expected_index_dict]
        modified_indexes = []
        for curr_idx in current_indexes:
            for exp_idx in expected_indexes:
                if curr_idx[0] == exp_idx[0] and curr_idx[1] != exp_idx[1]:
                    modified_indexes.append((curr_idx, exp_idx))
        
        return {
            'added': added_indexes,
            'removed': removed_indexes,
            'modified': modified_indexes
        }

    def add_indexes(self, database_name: str, table_name: str, current_indexes: list, expected_indexes: list):
        """
        Add indexes to the table based on expected indexes.
        """
        current_index_dict = {index[0]: index[1].strip(',') for index in current_indexes}
        
        for expected_index in expected_indexes:
            expected_index_name, expected_index_definition = expected_index
            expected_index_definition = expected_index_definition.strip(',')
       
            if expected_index_name not in current_index_dict:
                create_index_query = f"""
                    ALTER TABLE {database_name}.{table_name}
                    ADD INDEX {expected_index_name} {expected_index_definition}
                """
                logger.info(f"Adding index: {expected_index_name} - {expected_index_definition}")
                self.ch_client.execute(create_index_query)
                logger.info(f"Index '{expected_index_name}' added successfully")
            elif current_index_dict[expected_index_name] != expected_index_definition:
                logger.info(f"Index '{expected_index_name}' exists with a different definition. Would need to drop and recreate.")

    def remove_indexes(self, database_name: str, table_name: str, current_indexes: list, expected_indexes: list):
        """
        Remove indexes from the table that are present in the current schema 
        but not in the expected schema.
        """
        expected_index_names = [index[0] for index in expected_indexes]
        
        for current_index in current_indexes:
            current_index_name = current_index[0]
            
            if current_index_name not in expected_index_names:
                drop_index_query = f"""
                    ALTER TABLE {database_name}.{table_name}
                    DROP INDEX {current_index_name}
                """
                self.ch_client.execute(drop_index_query)
                logger.info(f"Index '{current_index_name}' has been successfully dropped.")

    def exchange_tables(self, database_name: str, table_name: str):
        """
        Exchange the original table with the replacement table.
        """
        logger.info(f"Initiating exchange between tables: {database_name}.{table_name} and {database_name}.replacement_{table_name}...")
        exchange_query = f"""
            EXCHANGE TABLES {database_name}.{table_name} AND {database_name}.replacement_{table_name}
        """
        self.ch_client.execute(exchange_query)
        logger.info(f"Successfully exchanged tables: {database_name}.{table_name} and {database_name}.replacement_{table_name}.")


    def insert_records_into_new_table(self, database_name: str, table_name: str, query_id: str) -> None:
        """
        Insert records from the old table into the new base table.

        Parameters:
            database_name (str): The name of the database.
            table_name (str): The name of the table.
        """
        logger.info(f"Starting record insertion into base table: {database_name}.{table_name}...")
        insert_query = f"""
            INSERT INTO {database_name}.{table_name}
            SELECT * FROM {database_name}.replacement_{table_name}
            SETTINGS min_insert_block_size_rows = {self.min_insert_block_size_rows},
                     max_insert_threads = {self.max_insert_threads};
        """

        self.ch_client.execute(insert_query, query_id=query_id)


    def validate_data_migration(self, database_name: str, table_name: str):
        """
        Validate that data migration was successful.
        """
        logger.info(f"Validating data migration for table: {database_name}.{table_name}...")
        new_record_count = self.check_table_records(database_name, table_name)
        old_record_count = self.check_table_records(database_name, f'replacement_{table_name}')
        if new_record_count >= old_record_count:
            logger.info(f"Data migration validated successfully:")
            logger.info(f"{new_record_count} records in {database_name}.{table_name}.")
            logger.info(f"{old_record_count} records in {database_name}.replacement_{table_name}.")
            return True
        else:
            logger.warning(f"Whilst validating data migration, expected a minimum {old_record_count} records in '{database_name}.{table_name}', but found {new_record_count} records.")
            return False


    def poll_data_migration_job(self, query_id: str, polling_interval: int) -> None:
        """
        Validate that data migration was successful.
        """
        logger.info(f"Polling 'system.processes' for query_id '{query_id}' every {polling_interval} seconds...")
        total_execution_time = 0
        still_running = self.check_system_processes_table(query_id)
        while still_running:
            time.sleep(polling_interval)
            total_execution_time += polling_interval
            still_running = self.check_system_processes_table(query_id)
            if not(still_running):
                logger.info(f"The query_id '{query_id}' is no longer present in 'system.processes' indicating the migration is complete.")
                break
            logger.info(f"\nThe migration job is still present in 'system.processes' after {total_execution_time} seconds. Polling again in {polling_interval} seconds...")
        logger.info(f"\nThe migration job is no longer present in 'system.processes'.")
        present_in_query_log = self.check_system_query_log_table(query_id)
        if (present_in_query_log):
            logger.info(f"Polling of 'system.processes' has completed successfully. Total time for stage to complete: {total_execution_time} seconds.")


    def drop_replacement_table(self, database_name: str, table_name: str):
        """
        Drop the old replacement table after successful migration.
        """
        logger.info(f"Dropping old replacement table: {database_name}.replacement_{table_name}...")
        drop_query = f"DROP TABLE IF EXISTS {database_name}.replacement_{table_name};"
        self.ch_client.execute(drop_query)
        logger.info(f"Replacement table {database_name}.replacement_{table_name} dropped successfully.")


    def get_existing_schema_ddl(self, database_name: str, table_name: str):
        """
        Get the existing schema DDL for the table.

        :return: The schema DDL statement for the specified table, or a dict with error info if error occurs
        """
        try:
            query = f"SHOW CREATE TABLE {database_name}.{table_name}"
            result = self.ch_client.execute(query)
            if result:
                return result[0][0]
            else:
                logger.error(f"Failed to retrieve schema for table: {table_name}")
                return ""
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Error getting DDL for table {database_name}.{table_name}: {parsed_error['user_message']} (Code: {parsed_error.get('code', 'Unknown')})")
            # Return a dict with error information instead of None
            return {
                'error': True,
                'error_type': parsed_error['type'],
                'error_message': parsed_error['user_message'],
                'error_code': parsed_error.get('code'),
                'error_details': parsed_error['message']
            }

    def check_table_records(self, database_name: str, table_name: str):
        """
        Check the number of records in the specified table.

        :param database_name: The name of the database.
        :param table_name: The name of the table to check.
        :return: The number of records in the table.
        """
        query = f"SELECT COUNT(*) AS count FROM {database_name}.{table_name}"
        logger.info(f"Checking record counts for: {database_name}.{table_name}")
        result = self.execute_query(query)
        if isinstance(result, list) and result:
            if isinstance(result[0], tuple):
                return result[0][0]
            elif isinstance(result[0], dict) and 'count' in result[0]:
                return result[0]['count']
        
        return 0
    
    def check_system_processes_table(self, query_id: str) -> bool:
        """
        Checks the system.processes table and returns a boolean value if the query_id provided is present

        :param query_id: The query_id to find to check.
        :return: A boolean value indicating if the query_id is present in system.processes or not
        """

        query = f"""
        SELECT
            formatReadableSize(memory_usage) AS memory_usage,
            formatReadableSize(peak_memory_usage) AS peak_memory_usage,
            ProfileEvents['OSCPUVirtualTimeMicroseconds'] AS vCPU_time_ms
        FROM
            clusterAllReplicas(default, system.processes)
        WHERE
            query_id = '{query_id}'
        """
        logger.info(f"\nQuerying 'system.processes' for query_id = '{query_id}'...")
        result = self.execute_query(query)
        
        if not(isinstance(result, list) and result):
            return False
    
        result_dict = {}
        result_dict['memory_usage'] = result[0][0]
        result_dict['peak_memory_usage'] = result[0][1]
        result_dict['vCPU_time_ms'] = result[0][2]

        for key, value in result_dict.items():
            logger.info(f"{key}: {value}")
        return True


    def check_system_query_log_table(self, query_id: str) -> bool:
        """
        Checks the system.query_log table and returns a boolean value if the query_id provided is present

        :param query_id: The query_id to find to check.
        :return: A boolean value indicating if the query_id is present in system.query_id or not
        """

        query = f"""
        SELECT
            exception,
            query_duration_ms,
            read_rows,
            written_rows
        FROM
            clusterAllReplicas(default, system.query_log)
        WHERE
            query_id = '{query_id}' AND type = 2
        """
        logger.info(f"\nQuerying 'system.query_log' for query_id = '{query_id}'...")

        result = self.execute_query(query)
        retry_count = 1

        while (not(isinstance(result, list) and result) and retry_count < 5):
            logger.info(f"The query_id '{query_id} was not found in the 'system.query_log'. Trying again in 5 seconds...")
            time.sleep(5)
            result = self.execute_query(query)
            retry_count += 1
        
        if not(isinstance(result, list) and result):
            logger.warning(f"After 5 attempts, the query_id '{query_id} was still not found in the 'system.query_log'.")
            return False
        
        result_dict = {}
        result_dict['exception'] = result[0][0]
        result_dict['query_duration_ms'] = result[0][1]
        result_dict['read_rows'] = result[0][2]
        result_dict['written_rows'] = result[0][3]
        
        if (result_dict['exception']):
            logger.warning(f"The query_id '{query_id} was found to contain an exception. {result_dict['exception']}.")
            return False
        
        for key, value in result_dict.items():
            if (value):
                logger.info(f"{key}: {value}")
        return True


    def execute_query(self, query: str):
        """
        Execute a query on the ClickHouse database and return the result.

        :param query: The query to execute.
        :return: The result of the query execution.
        """
        try:
            result = self.ch_client.execute(query)
            return result
        except Exception as e:
            parsed_error = ClickHouseErrorHandler.parse_error(e)
            logger.error(f"Query execution failed: {parsed_error['user_message']}")
            return None

    def normalize_data_type(self, data_type: str) -> str:
        """
        Normalize data type names to handle equivalent types.
        
        Args:
            data_type (str): The data type to normalize
            
        Returns:
            str: The normalized data type name
        """
        type_equivalents = {
            'Nullable(Bool)': 'Nullable(Boolean)',
            'Bool': 'Boolean',
            'Nullable(Boolean)': 'Nullable(Boolean)',
            'Boolean': 'Boolean'
        }
        
        return type_equivalents.get(data_type, data_type)

    def compare_columns_and_data_types(self, columns_with_existing_datatypes: dict, columns_with_new_datatypes: dict, database_name: str, table_name: str) -> list:
        """
        Compare columns and their data types between existing and new columns.
        Handles equivalent data types like Bool/Boolean.
        
        Args:
            existing_columns (dict): Existing columns with their data types.
            new_columns (dict): New columns with their data types.
            database_name (str): Name of the database
            table_name (str): Name of the table
        """
        for column, new_data_type in columns_with_new_datatypes.items():
            if column in columns_with_existing_datatypes:
                existing_type = self.normalize_data_type(columns_with_existing_datatypes[column])
                new_type = self.normalize_data_type(new_data_type)
                
                if existing_type != new_type:
                    logger.error(
                        f"Data type mismatch detected for table '{database_name}.{table_name}'. "
                        f"Manual intervention required for column '{column}'. "
                        f"Existing data type: '{columns_with_existing_datatypes[column]}', "
                        f"new data type: '{new_data_type}'. Please handle the data type change manually."
                    )
    
    def get_columns_with_types(self, sql_command: str) -> dict:
        columns = {}
        ddl_part = re.search(r'\((.*?)\)\s*ENGINE', sql_command, re.DOTALL)
        if ddl_part:
            for line in ddl_part.group(1).splitlines():
                line = line.strip()
                if line and not line.startswith('--'):
                    column_def = line.split()
                    if len(column_def) >= 2:
                        column_name = column_def[0]
                        column_type = column_def[1].replace(' ', '').rstrip(',')
                        columns[column_name] = column_type
        return columns
    
    def check_nullable_primary_key(self, database_name: str, table_name: str, primary_key: str) -> bool:
        """
        Check if any columns in the primary key are nullable.
        
        Args:
            database_name (str): Name of the database
            table_name (str): Name of the table
            primary_key (str): Primary key definition
            
        Returns:
            bool: True if any primary key column is nullable, False otherwise
        """
        def remove_cityhash(key_expr):
            return re.sub(r'cityHash64\s*\(\s*timestamp_load\s*\),?\s*', '', key_expr, flags=re.IGNORECASE).strip().strip(',')
        primary_key = remove_cityhash(primary_key)
        
        query = f"""
            SELECT name, type 
            FROM system.columns 
            WHERE database = '{database_name}' 
            AND table = '{table_name}'
        """
        column_info = self.ch_client.execute(query)
        column_types = {row[0]: row[1] for row in column_info}
        primary_key_columns = [col.strip() for col in primary_key.split(',')]
        for column in primary_key_columns:
            if column in column_types:
                if column_types[column].startswith('Nullable'):
                    logger.error(f"Column '{column}' in primary key is nullable with type '{column_types[column]}'")
                    return True
            else:
                logger.error(f"Column '{column}' not found in table '{database_name}.{table_name}'")
                return True

        return False

    def get_existing_columns_with_types(self, database_name: str, table_name: str) -> dict:
        query = f"DESCRIBE TABLE {database_name}.{table_name}"
        with self.clickhouse_manager.get_clickhouse_client() as ch_client:
            result = ch_client.execute(query)
            columns = {row[0]: row[1].replace(' ','') for row in result}
        return columns

    def parse_columns_from_ddl(self, ddl: str) -> list:
        """
        Parse column names from the DDL.

        Args:
            ddl (str): DDL statement for creating the table.

        Returns:
            list: A list of column names.
        """
        columns_part = re.search(r'\((.*?)\)\s*ENGINE', ddl, re.DOTALL).group(1)
        columns = []
        current_column = ""
        in_column_definition = False

        in_projection = False
        projection_paren_count = 0
        
        for line in columns_part.splitlines():
            line = line.strip()
            if not line or line.startswith('--'):
                continue
            if line.endswith(','):
                line = line[:-1]
            
            if line.startswith('INDEX '):
                continue
            
            if line.startswith('PROJECTION '):
                in_projection = True
                projection_paren_count = line.count('(') - line.count(')')
                continue
            
            if in_projection:
                projection_paren_count += line.count('(') - line.count(')')
                if projection_paren_count <= 0:
                    in_projection = False
                    projection_paren_count = 0
                continue
            
            if not in_column_definition:
                current_column = line
                if '(' in line and ')' not in line:
                    in_column_definition = True
            else:
                current_column += " " + line
                if ')' in line:
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
                    logger.warning(f"Failed to describe table {database_name}.{table_name}: {parsed_error['user_message']}")
                    return []
            else:
                logger.warning(f"Table {database_name}.{table_name} does not exist.")
                return []

    def generate_alter_statements(self, existing_columns: list, new_columns: list, database_name: str, table_name: str, ddl: str) -> list:
        """
        Generate ALTER statements for columns that are in new_columns but not in existing_columns.

        Args:
            existing_columns (list): List of existing columns in the table.
            new_columns (list): List of columns in the new DDL.
            database_name (str): Name of the database.
            table_name (str): Name of the table.
            ddl (str): The DDL statement for creating the table.

        Returns:
            list: A list of ALTER TABLE statements to add new columns.
        """
        different_columns = [column for column in new_columns if column not in existing_columns]
        different_columns = list(set(different_columns))

        final_alter_statement=[]
        if different_columns:
            ddl_lines = ddl.splitlines()
            column_definitions = []
            
            for column in different_columns:
                for line in ddl_lines:
                    if line.strip().startswith(column + ' ') and 'INDEX' not in line and 'PROJECTION' not in line:
                        column_definition = line.strip().rstrip(',')
                        column_definitions.append(column_definition)
                        break

            if len(column_definitions) > 0:
                alter_statement = f"ALTER TABLE {database_name}.{table_name} ADD COLUMN {', ADD COLUMN '.join(column_definitions)}"
                final_alter_statement.append(alter_statement) 
                
        return final_alter_statement
