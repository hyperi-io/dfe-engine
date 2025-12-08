"""Schema utility functions for the DFE data engine."""

from hs_lib.logger import logger
from pathlib import Path
import re
import os

from typing import Any, Iterator, List, Set, Tuple, Dict, Optional
import fnmatch
from importlib import resources
import pandas as pd


class SchemaValidationError(Exception):
    """
    Custom exception for schema validation errors.

    Attributes:
        message (str): Explanation of the error.
        errors (Optional[Exception]): Original exception, if available.
    """

    def __init__(self, message: str, errors: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors


class CSVValidationError(Exception):
    """
    Custom exception for schema validation errors.

    Attributes:
        message (str): Explanation of the error.
        errors (Optional[Exception]): Original exception, if available.
    """

    def __init__(self, message: str, errors: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors


class SchemaNameConflictError(Exception):
    """Custom exception for schema naming conflicts, especially for auto-generated columns."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class SchemaExceptions:
    class FileWriteError(Exception):
        def __init__(self, message: str):
            self.message = message
            super().__init__(self.message)

    class FileCloseError(Exception):
        def __init__(self, filename: str, message: str):
            self.filename = filename
            self.message = message
            super().__init__(f"Failed to close file '{self.filename}': {self.message}")

    class FileBackupError(Exception):
        def __init__(self, source_filename: str, message: str):
            self.source_filename = source_filename
            self.message = message
            super().__init__(
                f"Failed to backup file '{self.source_filename}': {self.message}"
            )

    class SchemaValidationError(Exception):
        def __init__(self, message: str):
            self.message = message
            super().__init__(self.message)


class SchemaUtils:
    __SQL_FILES_TO_IGNORE = [
        "create_databases.sql",
        "create_roles.sql",
        "create_service_accounts.sql",
    ]
    COLUMNS_TYPE_MAPS = [
        "type",
        "clickhouse_type",
        "clickhouse_type_index",
        "comment",
    ]

    COLUMNS_SCHEMA = [
        "column",
        "type",
        "default",
        "index_order",
        "index_type",
        "comment",
    ]
    COLUMNS_SUB_SCHEMA = ["column", "index_order"]
    COLUMNS_TYPE_MAPS_ES = ["es_type", "type"]
    COLUMNS_UNFIED_SCHEMA = [
        "source_device_schema",
        "source_field_name",
        "target_device_schema",
        "target_field_name",
        "description",
    ]

    @staticmethod
    def file_write(file_handle, content):
        try:
            file_handle.write(content)
        except Exception as e:
            raise Exception(f"Error writing to the file: {e}")

    @staticmethod
    def file_open(filename, mode):
        try:
            return open(filename, mode)
        except Exception as e:
            raise Exception(f"Error opening the file: {e}")

    @staticmethod
    def file_close(file_handle):
        try:
            file_handle.close()
        except Exception as e:
            raise Exception(f"Error closing the file: {e}")

    @staticmethod
    def create_path(create_path: str):
        try:
            os.makedirs(create_path, exist_ok=True)
        except OSError as e:
            raise Exception(
                f"Could not create schema output path: {create_path}. Error: {e}"
            )

    @staticmethod
    def read_customer_list(
        organisations: List[Dict[str, str]], logger: Any
    ) -> Dict[str, Dict[str, str]]:
        """
        Read the customer list from the input organisations list.

        Parameters:
            organisations (List[Dict[str, str]]): List of organisation dictionaries.
            logger (Any): Logger instance for logging messages.

        Returns:
            Dict[str, Dict[str, str]]: Dictionary containing customer information.
        """
        customer_data = {}
        try:
            for organisation in organisations:
                org_id = organisation.get("org_id", "default_orgid")
                cluster_name = organisation.get("cluster_name", "default_cluster")
                customer_data[org_id] = {"org_id": org_id, "cluster_name": cluster_name}
        except Exception as e:
            logger.error(
                f"An error occurred while reading the customer list: {e}", exc_info=True
            )
            raise
        return customer_data

    @staticmethod
    def walk_schema_directory(
        dfe_output_directory: str,
    ) -> Iterator[Tuple[str, List[str], List[str]]]:
        """
        Walk through the schema directory.

        Parameters:
            dfe_output_directory (str): The directory to walk through.

        Yields:
            Iterator[Tuple[str, List[str], List[str]]]: An iterator of directory paths,
            directory names, and filenames.
        """
        return os.walk(dfe_output_directory)

    @staticmethod
    def filter_schema_files(
        org_id: str,
        root: str,
        files: List[str],
        schema_filter_list: Optional[str],
        derived_schema_filter_list: Optional[str] = None,
        schema_filter_wildchar: Optional[str] = None,
        derived_schema_filter_wildchar: Optional[str] = None,
    ) -> List[Tuple[str, str, str]]:
        """
        Filter SQL schema files against the specified schema and sub-schema filter lists,
        as well as wildcard filters.

        Parameters:
            org_id (str): Organisation ID.
            root (str): Root directory.
            files (List[str]): List of files in the directory.
            schema_filter_list (Optional[str]): Comma-separated list of schema filters.
            derived_schema_filter_list (Optional[str]): Comma-separated list of sub-schema filters.
            schema_filter_wildchar (Optional[str]): Wildcard for matching schemas.
            derived_schema_filter_wildchar (Optional[str]): Wildcard for matching sub-schemas.

        Returns:
            List[Tuple[str, str, str]]: List of tuples containing (org_id, root, file_name).
        """
        schema_keys = set(schema_filter_list.split(",")) if schema_filter_list else None
        derived_schema_keys = (
            set(derived_schema_filter_list.split(","))
            if derived_schema_filter_list
            else None
        )

        filtered_files = []

        def _find_matching_schemas(base_name: str) -> bool:
            """
            Checks if the base_name matches any schema or sub-schema filters.
            Returns True if no filters are provided.
            """
            if not (
                schema_keys
                or derived_schema_keys
                or schema_filter_wildchar
                or derived_schema_filter_wildchar
            ):
                return True

            exact_schema_match = base_name in schema_keys if schema_keys else False
            exact_derived_schema_match = (
                base_name in derived_schema_keys if derived_schema_keys else False
            )
            wildcard_schema_match = (
                fnmatch.fnmatch(base_name, schema_filter_wildchar)
                if schema_filter_wildchar
                else False
            )
            wildcard_derived_schema_match = (
                fnmatch.fnmatch(base_name, derived_schema_filter_wildchar)
                if derived_schema_filter_wildchar
                else False
            )

            return (
                exact_schema_match
                or exact_derived_schema_match
                or wildcard_schema_match
                or wildcard_derived_schema_match
            )

        for file_name in files:
            if file_name in SchemaUtils.__SQL_FILES_TO_IGNORE or not file_name.endswith(
                ".sql"
            ):
                continue

            base_name, _ = os.path.splitext(file_name)
            if _find_matching_schemas(base_name):
                filtered_files.append((org_id, root, file_name))

        return filtered_files

    @staticmethod
    def extract_keys_and_indexes_from_ddl(ddl_statement: str) -> tuple:
        """
        Extract primary key, order by, indexes, TTL, sample by, partition by, projection, and table settings from DDL statement.
        """

        def extract_clause(pattern, text, skip_column_definitions=False):
            """
            Extract a clause like PRIMARY KEY or ORDER BY from DDL.
            
            Args:
                pattern: Regex pattern to match
                text: DDL text to search
                skip_column_definitions: If True, only search after ENGINE clause to avoid matching 
                                        ORDER BY inside column definitions (like PROJECTION)
            """
            search_text = text
            offset = 0
            
            if skip_column_definitions:
                engine_match = re.search(r'\bENGINE\b', text, re.IGNORECASE)
                if engine_match:
                    offset = engine_match.start()
                    search_text = text[offset:]
            
            match = re.search(pattern, search_text, re.IGNORECASE)
            if not match:
                return None

            start_pos = match.end()
            while start_pos < len(search_text) and search_text[start_pos].isspace():
                start_pos += 1
            if start_pos >= len(search_text):
                return None
            
            if search_text[start_pos] == "(":
                start_pos += 1
                count = 1
                end_pos = start_pos

                for i in range(start_pos, len(search_text)):
                    if search_text[i] == "(":
                        count += 1
                    elif search_text[i] == ")":
                        count -= 1
                        if count == 0:
                            end_pos = i
                            break

                return search_text[start_pos:end_pos].strip()
            else:
                end_patterns = [
                    r'\n',
                    r'\bTTL\b',
                    r'\bSETTINGS\b',
                    r'\bSAMPLE\b',
                    r'\bPARTITION\b',
                    r'\bPRIMARY\b',
                    r'\bORDER\b',
                    r';'
                ]
                end_pattern = '|'.join(end_patterns)
                next_match = re.search(end_pattern, search_text[start_pos:], re.IGNORECASE)
                
                if next_match:
                    return search_text[start_pos:start_pos + next_match.start()].strip()
                else:
                    return search_text[start_pos:].strip()

        # Extract PRIMARY KEY and ORDER BY after ENGINE to avoid matching inside PROJECTION
        primary_key = extract_clause(r"PRIMARY\s+KEY", ddl_statement, skip_column_definitions=True)
        order_by_key = extract_clause(r"ORDER\s+BY", ddl_statement, skip_column_definitions=True)
        
        # Extract partition by
        partition_by = extract_clause(r"PARTITION\s+BY", ddl_statement)
        
        sample_by_match = re.search(
            r"SAMPLE\s+BY\s+([\w\d\(\),\s]+?)(?:\s+(?:SETTINGS|TTL|;)|\s*$)",
            ddl_statement,
            re.IGNORECASE | re.DOTALL,
        )
        sample_by = sample_by_match.group(1).strip() if sample_by_match else None

        index_pattern = re.compile(
            r"INDEX\s+(\w+)\s*(?:\(([^)]+)\)|(\S+))\s+TYPE\s+(\w+)(?:\((\d+)\))?(?:\s+GRANULARITY\s+(\d+))?",
            re.IGNORECASE,
        )
        ttl_pattern = re.compile(r"INTERVAL\s+(\d+)\s+DAY", re.IGNORECASE)

        ttl_value_match = ttl_pattern.search(ddl_statement)
        ttl_value = ttl_value_match.group(1).strip() if ttl_value_match else None

        # Extract projection
        projection_pattern = re.compile(
            r"PROJECTION\s+(\w+)\s*\((.*?)\)",
            re.IGNORECASE | re.DOTALL,
        )
        projection_match = projection_pattern.search(ddl_statement)
        projection = None
        if projection_match:
            projection_name = projection_match.group(1).strip()
            projection_details = projection_match.group(2).strip()
            projection = (projection_name, projection_details)

        # Extract table settings
        settings_pattern = re.compile(
            r"SETTINGS\s+(.*?)(?:;|\s*$)",
            re.IGNORECASE | re.DOTALL,
        )
        settings_match = settings_pattern.search(ddl_statement)
        table_settings = settings_match.group(1).strip() if settings_match else None

        indexes = []
        for match in index_pattern.finditer(ddl_statement):
            index_name = match.group(1).strip()
            index_column = match.group(2) if match.group(2) else match.group(3)
            index_column = index_column.strip()
            index_type = match.group(4).strip()

            type_param = match.group(5) or "0" if "set" in index_type.lower() else ""
            type_with_param = (
                f"{index_type}({type_param})" if type_param else index_type
            )
            granularity = match.group(6) or "1"

            index_details = (
                f"{index_column} TYPE {type_with_param} GRANULARITY {granularity}"
            )
            indexes.append((index_name, index_details))

        return primary_key, order_by_key, indexes, ttl_value, sample_by, partition_by, projection, table_settings

    @staticmethod
    def is_view(ddl_content: str) -> bool:
        """
        Check if the given DDL content represents a view.

        Parameters:
            ddl_content (str): The DDL content.

        Returns:
            bool: True if the DDL content represents a view, False otherwise.
        """
        return (
            "CREATE VIEW IF NOT EXISTS" in ddl_content or "CREATE VIEW" in ddl_content
        )

    @staticmethod
    def sql_column_fix_name(column_name: str) -> str:
        column_name = re.sub("[-.]", "_", column_name)
        column_name = re.sub("[^a-zA-Z0-9_]", "", column_name)
        return column_name.lower()

    @staticmethod
    def load_type_maps(
        package: str,
        resource_path: str,
        column_names: List[str],
        dup_column: str,
        logger: Optional[Any] = None,
    ) -> pd.DataFrame:
        """
        Load a type map CSV file from a resource package and validate its structure.

        Parameters:
            package (str): The package name where the resource is located.
            resource_path (str): Resource name within the package.
            column_names (List[str]): List of expected column names in the CSV.
            dup_column (str): Column name to check for duplicates.
            logger (Optional[Any]): Logger for logging messages.

        Returns:
            pd.DataFrame: DataFrame containing the loaded CSV data.

        Raises:
            CSVValidationError: If the CSV file is missing columns or contains duplicate values in the specified column.
        """
        try:
            df = SchemaUtils.load_pd_csv_from_resource(
                package=package, resource_path=resource_path
            )
        except Exception as error:
            error_msg = f"Error loading CSV from resource {package}/{resource_path}: {str(error)}"
            raise CSVValidationError(error_msg, errors=error)

        type_columns = sorted(column_names)
        cols_df = sorted(df.columns.tolist())

        if cols_df != type_columns:
            error_msg = (
                f"Type CSV file is missing or has additional columns. "
                f"Should contain: {', '.join(type_columns)}; file {resource_path}."
            )
            raise CSVValidationError(error_msg)

        df[dup_column] = df[dup_column].str.lower()

        if not df[dup_column].is_unique:
            duplicate_values = df[df[dup_column].duplicated(keep=False)].sort_values(
                dup_column
            )
            error_msg = (
                f"Duplicate {dup_column} provided in file {resource_path}\n"
                + duplicate_values.to_markdown()
            )
            raise CSVValidationError(error_msg)

        return df

    @staticmethod
    def load_derived_schema(
        derived_schema_full_path: str, name: str, logger: Any
    ):
        """
        Load and validate the sub-schema.

        Args:
            derived_schema_full_path (str): Path to the sub-schema CSV file.
            name (str): The name used for logging.
            logger (Logger): Logger instance for logging messages.

        Returns:
            pd.DataFrame or pd.NA: Loaded sub-schema DataFrame or pd.NA if the sub-schema doesn't exist.

        Raises:
            SchemaValidationError: If the sub-schema file is missing required columns or is invalid.
        """

        if not derived_schema_full_path:
            logger.info("Sub-schema file path is not provided.")
            return None

        if not os.path.isfile(derived_schema_full_path):
            raise SchemaValidationError(
                f"Sub-schema file not found: {derived_schema_full_path}"
            )

        derived_schema_raw_df = SchemaUtils.load_pd_csv(
            derived_schema_full_path, name, logger
        )
        derived_schema_columns = sorted(derived_schema_raw_df.columns.tolist())

        required_columns = set(SchemaUtils.COLUMNS_SUB_SCHEMA)
        derived_schema_columns = set(derived_schema_columns)

        if not required_columns.issubset(derived_schema_columns):
            missing_columns = required_columns - derived_schema_columns
            raise SchemaValidationError(
                f"Sub-schema CSV file is missing required columns. Missing: {', '.join(missing_columns)}; "
                f"file {derived_schema_full_path} includes: {', '.join(derived_schema_columns)}"
            )

        return derived_schema_raw_df

    @staticmethod
    def load_additional_fields(
        additional_fields_full_path: str, name: str, logger: Any
    ):
        """
        Load additional fields from the given path.

        Args:
            additional_fields_full_path (str): Path to the additional fields CSV file.
            name (str): The name used for logging.
            logger (Any): Logger instance for logging messages.

        Returns:
            Union[pd.DataFrame, pd._libs.missing.NAType]: Loaded additional fields DataFrame or pd.NA if the file doesn't exist.
        """

        if not additional_fields_full_path:
            logger.info("Additional Fields path is not provided.")
            return None

        if not os.path.isfile(additional_fields_full_path):
            raise SchemaValidationError(
                f"Additional fields file not found: {additional_fields_full_path}"
            )

        logger.debug(f"Loading additional schema from {additional_fields_full_path}")

        additional_schema_df: pd.DataFrame = SchemaUtils.load_pd_csv(
            additional_fields_full_path, name, logger
        )
        return additional_schema_df

    @staticmethod
    def apply_additional_fields(
        meta_schema_df: pd.DataFrame,
        additional_schema_df: pd.DataFrame,
        logger: Any,
    ) -> pd.DataFrame:
        """
        Apply additional fields to the core schema.

        Args:
            meta_schema_df (pd.DataFrame): The core schema DataFrame.
            additional_schema_df (Union[pd.DataFrame, pd._libs.missing.NAType]): The additional fields DataFrame or pd.NA if not available.
            logger (Any): Logger instance for logging messages.

        Returns:
            pd.DataFrame: Updated meta_schema_df with additional fields appended.
        """
        meta_schema_df = meta_schema_df.astype(object)
        additional_schema_df = additional_schema_df.astype(object)
        meta_schema_df = pd.concat(
            [meta_schema_df, additional_schema_df], ignore_index=True
        )

        SchemaUtils.drop_duplicates(meta_schema_df)

        logger.debug(
            f"Number of fields with additional schema added: {len(additional_schema_df)}"
        )

        return meta_schema_df

    @staticmethod
    def apply_derived_schema(meta_schema_df: pd.DataFrame, derived_schema_df: pd.DataFrame, logger: Any):
        """
        Apply the sub-schema to the core schema.

        Args:
            meta_schema_df (pd.DataFrame): The core schema DataFrame.
            derived_schema_df (pd.DataFrame or pd.NA): The sub-schema DataFrame or pd.NA if not available.
            logger (Logger): Logger instance for logging messages.

        Returns:
            pd.DataFrame: The updated meta_schema_df.
        """
        if derived_schema_df is pd.NA:
            logger.warning("No sub-schema to apply.")
            return meta_schema_df

        # Vectorized: Build field_set and parent_to_children mapping
        all_columns = pd.concat([meta_schema_df['column'], derived_schema_df['column']], ignore_index=True)
        field_set = set(all_columns)

        # Vectorized: Extract parent fields (fields containing '.')
        has_dot = all_columns.str.contains('.', regex=False)
        dotted_columns = all_columns[has_dot]
        parents = dotted_columns.str.split('.').str[0]

        # Build parent_to_children using groupby
        parent_to_children = {}
        if len(parents) > 0:
            parent_child_df = pd.DataFrame({'parent': parents, 'child': dotted_columns})
            for parent, group in parent_child_df.groupby('parent'):
                parent_to_children[parent] = set(group['child'])

        # Find fields to remove (parents with only one child that exist in field_set)
        fields_to_remove = {
            parent for parent, children in parent_to_children.items()
            if parent in field_set and len(children) == 1
        }

        result_df = meta_schema_df.copy()
        if fields_to_remove:
            result_df = result_df[~result_df['column'].isin(fields_to_remove)]

        meta_schema_df = result_df

        # Vectorized: Filter by wildcard and exact matches
        wildcard_mask = derived_schema_df['column'].str.endswith('*')
        wildcard_cols = derived_schema_df.loc[wildcard_mask, 'column'].str.rstrip('*').tolist()
        exact_cols = set(derived_schema_df.loc[~wildcard_mask, 'column'])

        # Build filter mask
        in_exact = meta_schema_df['column'].isin(exact_cols)
        starts_with_wildcard = meta_schema_df['column'].str.startswith(tuple(wildcard_cols)) if wildcard_cols else False
        meta_schema_df = meta_schema_df[in_exact | starts_with_wildcard].copy()  # Make a copy to avoid SettingWithCopyWarning

        # Vectorized: Check for valid index_order (numeric values)
        has_valid_index_order = False
        if 'index_order' in derived_schema_df.columns:
            # Check if any non-null values exist that are numeric
            idx_order = derived_schema_df['index_order']
            has_valid_index_order = idx_order.notna().any() and pd.api.types.is_numeric_dtype(idx_order.dropna())

        if has_valid_index_order:
            logger.debug('Clearing base schema index_order for sub-schema override')
            meta_schema_df = meta_schema_df.copy()
            meta_schema_df['index_order'] = meta_schema_df['index_order'].astype('Int64')
            meta_schema_df.loc[:, 'index_order'] = pd.NA

            # Merge only 'column' and 'index_order' - other columns are handled via override lookup
            merge_columns = ['column']
            if 'index_order' in derived_schema_df.columns:
                merge_columns.append('index_order')

            meta_schema_df = meta_schema_df.merge(
                derived_schema_df[merge_columns],
                on='column',
                how='left',
                suffixes=('', '_sub')
            )

            # Replace index_order with the merged values from derived schema
            if 'index_order_sub' in meta_schema_df.columns:
                meta_schema_df['index_order'] = meta_schema_df['index_order_sub']
                meta_schema_df.drop(columns=['index_order_sub'], inplace=True)

            # Vectorized override: use derived_schema_df for lookups (not merged columns)
            for col in ['index_type', 'type', 'index_order', 'default']:
                if col in derived_schema_df.columns:
                    # Build mapping from column name to override value using original derived_schema_df
                    derived_subset = derived_schema_df[['column', col]].dropna(subset=[col])
                    # Filter out empty strings
                    derived_subset = derived_subset[derived_subset[col].astype(str).str.strip() != '']
                    if len(derived_subset) > 0:
                        override_map = dict(zip(derived_subset['column'], derived_subset[col]))
                        # Apply override using list comprehension (faster than apply, handles mixed types)
                        original_values = meta_schema_df[col].tolist()
                        column_names = meta_schema_df['column'].tolist()
                        new_values = [
                            override_map.get(col_name, orig_val)
                            for col_name, orig_val in zip(column_names, original_values)
                        ]
                        meta_schema_df[col] = new_values
        else:
            logger.warning('No valid index_order values found in derived schema.')

            # Vectorized override for non-index_order columns
            for col in ['index_type', 'type', 'default']:
                if col in derived_schema_df.columns:
                    derived_subset = derived_schema_df[['column', col]].dropna(subset=[col])
                    derived_subset = derived_subset[derived_subset[col].astype(str).str.strip() != '']
                    if len(derived_subset) > 0:
                        override_map = dict(zip(derived_subset['column'], derived_subset[col]))
                        # Apply override using list comprehension
                        original_values = meta_schema_df[col].tolist()
                        column_names = meta_schema_df['column'].tolist()
                        new_values = [
                            override_map.get(col_name, orig_val)
                            for col_name, orig_val in zip(column_names, original_values)
                        ]
                        meta_schema_df[col] = new_values

        meta_schema_df.reset_index(drop=True, inplace=True)
        SchemaUtils.drop_duplicates(meta_schema_df)

        return meta_schema_df

    @staticmethod
    def get_resource_path(package: str, resource_path: str):
        """
        Get the path-like object for the specified resource.

        Parameters:
            package (str): Package name where the resource is located.
            resource_path (str): Resource name within the package.

        Returns:
            Path: Path-like object for the resource.

        Raises:
            SchemaValidationError: If the resource does not exist.
        """
        try:
            package_path = resources.files(package)
            return package_path / resource_path
        except FileNotFoundError as e:
            raise SchemaValidationError(
                f"Resource does not exist: {package}/{resource_path}", errors=e
            )

    @staticmethod
    def load_schema_from_directory(schema_file_path: Path) -> pd.DataFrame:
        """
        Load schema from an external directory.

        Args:
            schema_file_path (path): Path to the external directory.
        Returns:
            pd.DataFrame: Loaded schema as a DataFrame.
        """

        try:
            df = pd.read_csv(schema_file_path)
            if "index_type" not in df.columns:
                df["index_type"] = ""
            if "default" not in df.columns:
                df["default"] = ""
            return df
        except FileNotFoundError:
            raise SchemaValidationError(f"Schema file not found: {schema_file_path}")
        except pd.errors.EmptyDataError:
            raise SchemaValidationError(f"No data in CSV file: {schema_file_path}")
        except pd.errors.ParserError as e:
            raise SchemaValidationError(
                f"Parser error when reading CSV file: {schema_file_path}", errors=e
            )
        except Exception as e:
            raise SchemaValidationError(
                f"Failed to read CSV: {schema_file_path}", errors=e
            )

    @staticmethod
    def load_pd_csv_from_resource(package: str, resource_path: str) -> pd.DataFrame:
        """
        Loads a CSV from a Python package resource using Pandas.

        :param package: Package name where the resource is located.
        :param resource: Resource name within the package.
        :return: Pandas DataFrame loaded from the CSV.
        """
        resource_path_object = SchemaUtils.get_resource_path(package, resource_path)

        try:
            return pd.read_csv(resource_path_object)
        except FileNotFoundError:
            raise SchemaValidationError(f"Resource file not found: {resource_path}")
        except pd.errors.EmptyDataError:
            raise SchemaValidationError(f"No data in CSV file: {resource_path}")
        except pd.errors.ParserError as e:
            raise SchemaValidationError(
                f"Parser error when reading CSV file: {resource_path}", errors=e
            )
        except Exception as e:
            raise SchemaValidationError(
                f"Failed to read CSV: {resource_path}", errors=e
            )

    @staticmethod
    def dict_remove_key_recursively(dict_obj, remove_key):
        if isinstance(dict_obj, dict):
            for key in list(dict_obj.keys()):
                if key == remove_key:
                    del dict_obj[key]
                else:
                    SchemaUtils.dict_remove_key_recursively(dict_obj[key], remove_key)
        return

    @staticmethod
    def load_pd_csv(
        csv_filename: str, schema_name: str, logger: Any
    ) -> pd.DataFrame:
        """
        Load a CSV file into a Pandas DataFrame.

        Parameters:
            csv_filename (str): The filename (including path) of the CSV to load.
            schema_name (str): The name of the schema being loaded.

        Returns:
            pd.DataFrame: The loaded DataFrame.

        Raises:
            SchemaValidationError: If there is an error loading the CSV file.
        """
        try:
            return pd.read_csv(csv_filename)
        except Exception as error:
            error_msg = f"Unable to load CSV {schema_name} from file {csv_filename}."
            if hasattr(error, "strerror"):
                error_msg += f" Error: {error.strerror}"

            logger.error(error_msg, exc_info=True)

            raise SchemaValidationError(error_msg, errors=error)

    @staticmethod
    def drop_flattened_schema_duplicates(df: pd.DataFrame) -> pd.DataFrame:
        df = df.groupby(
            df["column"].str.lower().replace(".", "_"), as_index=False, sort=False
        ).last()
        return df

    @staticmethod
    def drop_duplicates(df: pd.DataFrame):
        return df.groupby("column", as_index=False, sort=False).last()

    @staticmethod
    def schema_column_name_valid(column_name: str):
        return re.search("[a-zA-Z0-9_\\-.]", column_name)

    @staticmethod
    def load_schema_from_resource_package(package_path: str, resource_path: str):
        df = SchemaUtils.load_pd_csv_from_resource(package_path, resource_path)
        if "index_type" not in df.columns:
            df["index_type"] = ""
        if "default" not in df.columns:
            df["default"] = ""

        return df

    @staticmethod
    def schema_check(
        df_to_check: pd.DataFrame,
        src_schema_columns: List[str],
        source_filename: str,
        type_map_df: pd.DataFrame,
        logger: Any,
    ) -> None:
        required_columns = set(src_schema_columns)
        actual_columns = set(df_to_check.columns.tolist())

        # Check for missing required columns (error)
        missing_columns = required_columns - actual_columns
        if missing_columns:
            msg = (
                f"Schema CSV file is missing required columns: {', '.join(sorted(missing_columns))}; "
                f"File: {source_filename}."
            )
            logger.error(msg)
            raise SchemaValidationError(msg)

        # Check for extra columns (info only - for backward compatibility with OpenSearch fields like search_order)
        extra_columns = actual_columns - required_columns
        if extra_columns:
            logger.info(
                f"Schema CSV file '{source_filename}' contains extra columns that will be ignored: "
                f"{', '.join(sorted(extra_columns))}. This is allowed for backward compatibility."
            )

        df_to_check["type"] = df_to_check["type"].str.lower()

        duplicate_columns = df_to_check["column"][
            df_to_check["column"].duplicated()
        ].unique()
        if duplicate_columns.size > 0:
            msg = (
                f"Duplicate column names provided in file {source_filename}: "
                f"{', '.join(duplicate_columns)}."
            )
            logger.error(msg)
            raise SchemaValidationError(msg)

        column_set = set(df_to_check["column"])
        warnings = []

        for column_name, column_type in zip(df_to_check["column"], df_to_check["type"]):
            upstream_keys = column_name.split(".")
            if len(upstream_keys) > 1:
                key_stage = ""
                for upstream_key in upstream_keys[:-1]:
                    key_stage = (
                        f"{key_stage}.{upstream_key}" if key_stage else upstream_key
                    )
                    if key_stage in column_set:
                        warnings.append(
                            f"Column [{column_name}] has an upstream key [{key_stage}] that can also contain a value. "
                            "This is not permitted."
                        )

            if column_type not in type_map_df["type"].values:
                msg = f'Invalid type "{column_name}:{column_type}" found in file {source_filename}.'
                logger.error(msg)
                raise SchemaValidationError(msg)

            if not SchemaUtils.schema_column_name_valid(column_name):
                msg = f"Schema CSV file has invalid characters in column name: {column_name}."
                logger.error(msg)
                raise SchemaValidationError(msg)

        for warning in warnings:
            logger.warning(warning)

    @staticmethod
    def clean_es_properties(dict_obj):
        """Remove non-dict entries from properties and recursively clean nested dicts."""
        if "properties" in dict_obj:
            for key in list(dict_obj.keys()):
                if not isinstance(dict_obj[key], dict):
                    del dict_obj[key]
        for key in dict_obj:
            if isinstance(dict_obj[key], dict):
                SchemaUtils.clean_es_properties(dict_obj[key])

    @staticmethod
    def manage_nested_dict_action(
        dict1: dict, key_path: str, action: str = "find", value: Optional[dict] = None
    ) -> Optional[bool]:
        """
        Traverse and manipulate a nested dictionary based on a dot-separated key path.

        Parameters:
            dict1 (dict): The dictionary to traverse.
            key_path (str): The dot-separated key path.
            action (str): The action to perform ('find', 'delete', 'get', 'set'). Default is 'find'.
            value (Optional[dict]): The value to set if action is 'set'. Default is None.

        Returns:
            Optional[bool]: True/False if action is 'find', 'delete', or 'set'. The value if action is 'get'.
                            False if the key path does not exist.
        """
        keys = key_path.split(".")
        sub_dict = dict1

        for idx, key in enumerate(keys):
            if idx < len(keys) - 1:
                if key in sub_dict:
                    sub_dict = sub_dict[key]
                elif action == "set":
                    sub_dict[key] = {}
                    sub_dict = sub_dict[key]
                else:
                    return False
            else:
                if action == "find":
                    return key in sub_dict
                elif action == "delete":
                    if key in sub_dict:
                        del sub_dict[key]
                        return True
                elif action == "get":
                    return sub_dict.get(key)
                elif action == "set":
                    sub_dict[key] = value
                    return True

        return False

    @staticmethod
    def dict_merge(
        dict1: dict,
        dict2: dict,
        path: Optional[List[str]] = None,
        overwrite: bool = False,
    ) -> dict:
        """
        Recursively merges dict2 into dict1.

        Parameters:
            dict1 (dict): The first dictionary to merge into.
            dict2 (dict): The second dictionary to merge from.
            path (Optional[List[str]]): The list of keys forming the current path.
            overwrite (bool): Flag to overwrite values in case of conflict.

        Returns:
            dict: The merged dictionary.

        Raises:
            SchemaValidationError: If there is a conflict and overwrite is False.
        """

        if path is None:
            path = []

        for key in dict2:
            if key in dict1:
                if isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
                    SchemaUtils.dict_merge(
                        dict1[key], dict2[key], path + [str(key)], overwrite
                    )
                elif dict1[key] != dict2[key]:
                    if overwrite:
                        dict1[key] = dict2[key]
                    else:
                        raise SchemaValidationError(
                            f"Conflict at {'.'.join(path + [str(key)])}"
                        )
            else:
                dict1[key] = dict2[key]

        return dict1

    @staticmethod
    def df_col_not_empty(df: pd.DataFrame, col_name: str):
        if (col_name in df) and (len(df[col_name].value_counts()) > 0):
            return True
        return False

    @staticmethod
    def strip_duplicate_config_columns_from_non_common_df(
        meta_schema_df: pd.DataFrame,
        common_header_schema_df: pd.DataFrame,
        schema_name: str,
        logger: Any,
        is_ch_flag: bool,
    ) -> pd.DataFrame:
        """
        Remove duplicate columns from the working DataFrame that are already in the common header schema.
        If duplicate columns are found, log a warning and remove them from the working DataFrame.

        Args:
            working_df (pd.DataFrame): The DataFrame containing schema configuration columns.
            common_header_schema_df (pd.DataFrame): The DataFrame containing common header schema columns.
            schema_name (str): Schema name for logging
            logger (Any): Logger instance for logging warnings.

        Returns:
            pd.DataFrame: The updated DataFrame with duplicate columns removed.
        """
        if is_ch_flag:
            common_header_schema_df["normalized_column"] = common_header_schema_df[
                "column"
            ].str.replace(".", "_", regex=False)
            meta_schema_df["normalized_column"] = meta_schema_df["column"].str.replace(
                ".", "_", regex=False
            )
        else:
            common_header_schema_df["normalized_column"] = common_header_schema_df[
                "column"
            ]
            meta_schema_df["normalized_column"] = meta_schema_df["column"]

        meta_dups = meta_schema_df["normalized_column"].duplicated(keep="first")
        if meta_dups.any():
            meta_dup_cols = meta_schema_df[meta_dups]["column"].tolist()
            logger.warning(
                f"{schema_name} has duplicate columns within meta schema: {meta_dup_cols}"
            )

        common_dups = common_header_schema_df["normalized_column"].duplicated(
            keep="first"
        )
        if common_dups.any():
            common_dup_cols = common_header_schema_df[common_dups]["column"].tolist()
            logger.warning(
                f"{schema_name} has duplicate columns within common schema: {common_dup_cols}"
            )

        meta_schema_df = meta_schema_df.drop_duplicates(
            subset="normalized_column", keep="first"
        )
        common_header_schema_df = common_header_schema_df.drop_duplicates(
            subset="normalized_column", keep="first"
        )

        duplicates = meta_schema_df["normalized_column"].isin(
            common_header_schema_df["normalized_column"]
        )
        duplicates_columns = meta_schema_df[duplicates]["column"].tolist()

        if duplicates.any():
            logger.warning(
                f"{schema_name} contains columns that already exist in common header: {duplicates_columns}. Common header values will be used."
            )

        meta_schema_df_cleaned = meta_schema_df[~duplicates].drop(
            columns="normalized_column"
        )
        common_header_schema_df = common_header_schema_df.astype(object)
        meta_schema_df_cleaned = meta_schema_df_cleaned.astype(object)
        complete_schema_df = pd.concat(
            [common_header_schema_df, meta_schema_df_cleaned], ignore_index=True
        )
        complete_schema_df = complete_schema_df.drop_duplicates(
            subset="column", keep="first"
        )
        complete_schema_df.reset_index(drop=True, inplace=True)

        return complete_schema_df

    def flatten_properties(
        properties: Dict[str, Any],
        parent_key: str = "",
        sep: str = ".",
        property_key: str = "properties",
        include_multifields: bool = True,
    ) -> Dict[str, Any]:
        """
        Recursively flatten a nested dictionary starting at the "properties" key.

        Supports Elasticsearch template structures for parsing input templates:
        - Standard nested `properties` (e.g., {"user": {"properties": {"name": {"type": "keyword"}}}})
        - Multi-fields via `fields` key (e.g., {"message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}}})
        - Pattern with `fields` containing `properties` (e.g., {"name": {"properties": {"fields": {"properties": {...}}}}})

        Args:
            properties (Dict[str, Any]): The dictionary containing nested properties to be flattened.
            parent_key (str): The base key used to create fully qualified field names (default is an empty string).
            sep (str): The separator used to concatenate nested keys (default is '.').
            property_key (str): The key to look for nested properties (default is 'properties').
            include_multifields (bool): Whether to also traverse 'fields' for Elastic multi-fields (default is True).

        Returns:
            Dict[str, Any]: A flattened dictionary where keys are the full field paths and values are the original nested values.
        """
        flattened_items = {}

        for key, value in properties.items():
            new_key = f"{parent_key}{sep}{key}" if parent_key else key

            if not isinstance(value, dict):
                # Non-dict values are kept as-is
                flattened_items[new_key] = value
                continue

            has_nested_properties = property_key in value
            has_multifields = include_multifields and "fields" in value and isinstance(value.get("fields"), dict)

            if has_nested_properties:
                # Recurse into nested properties
                flattened_items.update(
                    SchemaUtils.flatten_properties(
                        value[property_key], new_key, sep=sep, include_multifields=include_multifields
                    )
                )
            elif has_multifields:
                # Handle multi-fields: add the parent field itself (without 'fields' key)
                # then recurse into the multi-fields
                parent_value = {k: v for k, v in value.items() if k != "fields"}
                if parent_value:
                    flattened_items[new_key] = parent_value

                # Recurse into multi-fields
                fields_dict = value["fields"]
                # Check if fields contains 'properties' or direct type definitions
                if "properties" in fields_dict:
                    # Pattern: {"fields": {"properties": {"text": {...}}}}
                    flattened_items.update(
                        SchemaUtils.flatten_properties(
                            fields_dict["properties"], f"{new_key}.fields", sep=sep, include_multifields=include_multifields
                        )
                    )
                else:
                    # Standard Elastic multi-fields: {"fields": {"keyword": {"type": "keyword"}}}
                    for field_name, field_def in fields_dict.items():
                        if isinstance(field_def, dict):
                            multifield_key = f"{new_key}{sep}{field_name}"
                            # Check if the multi-field itself has nested properties
                            if "properties" in field_def:
                                flattened_items.update(
                                    SchemaUtils.flatten_properties(
                                        field_def["properties"], multifield_key, sep=sep, include_multifields=include_multifields
                                    )
                                )
                            else:
                                flattened_items[multifield_key] = field_def
            else:
                # Leaf field - no nested properties or multi-fields
                flattened_items[new_key] = value

        return flattened_items

    def extract_field_paths(
        properties, parent_key: str = "", sep: str = "."
    ) -> Set[str]:
        """
        Recursively extract field paths from a dictionary representing nested properties.

        Args:
            properties (dict): The dictionary containing the properties.
            parent_key (str): The base key for nested fields (default is an empty string).
            sep (str): The separator to use for concatenating nested keys (default is '.').

        Returns:
            Set[str]: A set containing all flattened field paths.
        """
        field_paths = set()

        for key, value in properties.items():
            field_path = f"{parent_key}{sep}{key}" if parent_key else key
            if "properties" in value:
                field_paths.update(
                    SchemaUtils.extract_field_paths(
                        value["properties"], field_path, sep=sep
                    )
                )
            else:
                field_paths.add(field_path)

        return field_paths

    @staticmethod
    def normalize_dot_version(version: str) -> str:
        parts = version.split(".")
        normalized_parts = [part.zfill(3) for part in parts]
        return "_".join(normalized_parts)


def generate_norm_column_sql(
    column_name: str,
    column_type: str,
    index_type: Optional[str] = None
) -> Optional[str]:
    """
    Generate MATERIALIZED _norm column SQL for indexed ip_field columns.

    Args:
        column_name: Column name (e.g., "ip")
        column_type: Column type (e.g., "ip_field")
        index_type: Index type if present (e.g., "dimension")

    Returns:
        SQL for MATERIALIZED column, or None
    """
    # Check pd.notna first to avoid ambiguous boolean with pd.NA
    if column_type == 'ip_field' and pd.notna(index_type) and index_type:
        norm_col_name = f"{column_name}_norm"
        # Use accessor syntax (.IPv4, .IPv6) for Variant field extraction
        return (
            f"{norm_col_name} IPv6 MATERIALIZED "
            f"if(variantType({column_name})='IPv4', "
            f"IPv4ToIPv6({column_name}.IPv4), "
            f"{column_name}.IPv6)"
        )
    return None

