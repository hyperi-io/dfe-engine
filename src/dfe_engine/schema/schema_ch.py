from datetime import datetime

from pathlib import Path
import typing
import pandas as pd
import os
from typing import Optional
from .schema_util import (
    SchemaExceptions,
    SchemaUtils,
    SchemaValidationError,
    SchemaNameConflictError,
    generate_norm_column_sql,
)
from hs_lib.logger import logger


class ClickHouseSchema:
    """
    A class to generate ClickHouse schemas based on provided configurations.

    Attributes:
        name (str): Name of the schema.
        version (Optional[str]): Version of the schema.
        meta_schema_file_path (path): Path to the meta schema CSV.
        common_resource_path (str): Path to the common headers CSV.
        derived_schema_full_path (Optional[str]): Path to the sub-schema CSV.
        additional_fields_full_path (Optional[str]): Path to the additional fields CSV.
        dfe_output_path (str): Output directory for generated schemas.
        cluster_declarations_needed (bool): Include cluster declarations in the schema.
        use_replicated_merge_tree (bool): Use ReplicatedMergeTree engine.
        use_shared_merge_tree (bool): Use SharedMergeTree engine.
        ttl (int): Time-to-live in days for the data.
        logger (Optional[logging.Logger]): Logger instance.
    """

    PARTITION_BY_STATEMENT = "\nPARTITION BY toYYYYMMDD(timestamp_load) \n"
    TTL_STATEMENT = "TTL timestamp + INTERVAL {ttl} DAY DELETE WHERE timestamp >= 0, \n"
    TTL_STATEMENT_LOAD = "timestamp_load + INTERVAL {ttl} DAY DELETE WHERE timestamp_load >= 0 \n"
    PROJECTION_BY_TIMESTAMP = """PROJECTION timestamp_optimized ( SELECT * ORDER BY timestamp )"""
    TABLE_SETTINGS = """SETTINGS\n    index_granularity = 2048,\n    ttl_only_drop_parts = 1;\n"""
    ENGINE_REPLICATED = ")\nENGINE = ReplicatedMergeTree() "
    ENGINE_SHARED = ")\nENGINE = SharedMergeTree() "
    ENGINE_MERGE = ")\nENGINE = MergeTree()"

    def __init__(
        self,
        name: str,
        version: Optional[str],
        meta_schema_file_path: Path,
        common_resource_path: str,
        derived_schema_full_path: str = None,
        additional_fields_full_path: str = None,
        dfe_output_path: str = os.path.join(os.getcwd(), "dfeoutput"),
        no_cluster_declarations_needed: bool = True,
        use_replicated_merge_tree: bool = True,
        use_json_feature: bool = False,
        use_subsampling_feature: bool = False,
        use_shared_merge_tree: bool = True,
        ttl: int = 90,
        logger=None,
    ):
        # Schema identification
        self.schema_name = name
        self.version = version

        # Paths and packages
        self.meta_schema_file_path = meta_schema_file_path
        self.common_resource_path = common_resource_path
        self.derived_schema_full_path = derived_schema_full_path
        self.additional_fields_full_path = additional_fields_full_path
        self.commons_variables_package = "dfe_engine.resources"

        # Schema configurations
        self.no_cluster_declarations_needed = no_cluster_declarations_needed
        self.use_replicated_merge_tree = use_replicated_merge_tree
        self.use_json_feature = use_json_feature
        self.use_subsampling_feature = use_subsampling_feature
        self.use_shared_merge_tree = use_shared_merge_tree
        self.ttl = ttl

        # Logger setup

        # Internal state
        self.schema_output_path = os.path.join(dfe_output_path, self.schema_name)

        self.ch_schema_file = None
        self.has_derived_schema = False

        # Load necessary data
        self.type_map_df = SchemaUtils.load_type_maps(
            self.commons_variables_package,
            os.path.join(self.common_resource_path, "type_maps.csv"),
            SchemaUtils.COLUMNS_TYPE_MAPS,
            "type",
        )

        # Handle JSON feature logic with logging
        json_type_df = self.type_map_df[self.type_map_df["type"] == "json"]
        string_type_df = self.type_map_df[self.type_map_df["type"] == "string"]
        if not json_type_df.empty:
            logger.info("JSON type found in type map.")
            if not self.use_json_feature:
                logger.warning("use_json_feature is False, using string type mapping for JSON.")
                if not string_type_df.empty:
                    json_clickhouse_type = string_type_df.iloc[0]["clickhouse_type"]
                    json_clickhouse_type_index = string_type_df.iloc[0]["clickhouse_type_index"]
                    self.type_map_df.loc[self.type_map_df["type"] == "json", "clickhouse_type"] = (
                        json_clickhouse_type
                    )
                    self.type_map_df.loc[
                        self.type_map_df["type"] == "json", "clickhouse_type_index"
                    ] = json_clickhouse_type_index
                else:
                    logger.error("No string type found in type map. Check your type_maps.csv.")
            else:
                logger.info("Using existing JSON type from the type map.")
        else:
            logger.error("No JSON type found in type map. Check your type_maps.csv.")

        self.common_header_schema_df = SchemaUtils.load_schema_from_resource_package(
            self.commons_variables_package,
            os.path.join(self.common_resource_path, "common_header.csv"),
        )

        self.meta_schema_df = SchemaUtils.load_schema_from_directory(self.meta_schema_file_path)

        SchemaUtils.create_path(self.schema_output_path)

    def _validate_norm_column_names(self, schema_df: pd.DataFrame) -> None:
        """
        Validate that indexed ip_field columns don't conflict with user-defined _norm columns.

        Args:
            schema_df (pd.DataFrame): Schema DataFrame to validate

        Raises:
            SchemaNameConflictError if conflict detected
        """
        existing_cols = set(schema_df[schema_df["column"].notna()]["column"].tolist())

        for _, row in schema_df.iterrows():
            col_name = row.get("column")
            col_type = row.get("type")
            col_index_type = row.get("index_type")

            if col_type == "ip_field" and col_index_type:
                expected_norm_name = f"{col_name}_norm"

                if expected_norm_name in existing_cols:
                    error_msg = (
                        f"Conflict: '{expected_norm_name}' already exists. "
                        f"The ip_field '{col_name}' with index_type '{col_index_type}' "
                        f"requires auto-generating a MATERIALIZED column '{expected_norm_name}'. "
                        f"Please rename the manual column or remove the index_type."
                    )
                    raise SchemaNameConflictError(error_msg)

    # --- Helper methods for ip_field Variant handling --------------------------------
    def _is_ip_field_variant(self, column_type: str, clickhouse_type: str) -> bool:
        """
        Determine if a column represents an ip_field backed by a Variant(IPv4, IPv6) type.

        Args:
            column_type: logical schema type (e.g., 'ip_field')
            clickhouse_type: mapped ClickHouse type string from type_maps (e.g., 'Variant(IPv4, IPv6) CODEC(LZ4)')

        Returns:
            True if it's an ip_field mapped to a Variant IPv4/IPv6 ClickHouse type.
        """
        if not clickhouse_type:
            return False
        try:
            ch = str(clickhouse_type)
            if column_type == "ip_field":
                return True
            # Also accept cases where type maps to Variant but logical type differs
            if "Variant(IPv4" in ch or "Variant(IPv6" in ch:
                return True
        except (TypeError, AttributeError):
            return False
        return False

    def generate_norm_column_for_ip_field(self, column_name: str) -> str:
        """
        Generate the SQL for a MATERIALIZED _norm column for a given ip_field column.

        Args:
            column_name: base column name (e.g., 'src_ip')

        Returns:
            SQL snippet for the MATERIALIZED _norm column.
        """
        norm_column_name = f"{column_name}_norm"
        sql = (
            f"{norm_column_name} IPv6 MATERIALIZED "
            f"if(variantType({column_name})='IPv4', "
            f"IPv4ToIPv6({column_name}.IPv4), "
            f"{column_name}.IPv6)"
        )
        return sql

    def generate_index_for_norm_column(self, norm_column_name: str) -> str:
        """
        Generate minmax INDEX SQL for a _norm column.

        Args:
            norm_column_name: Name of the _norm column

        Returns:
            Index SQL string.
        """
        index_name = f"idx_{norm_column_name}_minmax"
        return f"INDEX {index_name} {norm_column_name} TYPE minmax GRANULARITY 1"

    def update_order_by_for_ip_fields(
        self, order_by_columns: list, schema_df: pd.DataFrame
    ) -> list:
        """
        Replace ip_field columns in the ORDER BY list with their corresponding _norm columns.

        Args:
            order_by_columns: list of column names currently chosen for ORDER BY
            schema_df: DataFrame of schema definitions (must include 'column' and 'type')

        Returns:
            Updated list of columns for ORDER BY, with ip_field columns replaced by <col>_norm.
        """
        # Build set of ip_field columns
        ip_cols = set()
        for _, r in schema_df.iterrows():
            col = r.get("column")
            typ = r.get("type")
            # resolve clickhouse_type from type_map if available
            type_match_df = self.type_map_df.loc[self.type_map_df["type"] == str(typ).lower()]
            clickhouse_type = ""
            if not type_match_df.empty:
                clickhouse_type = type_match_df.iloc[0]["clickhouse_type"]
            if self._is_ip_field_variant(str(typ).lower(), clickhouse_type):
                ip_cols.add(SchemaUtils.sql_column_fix_name(col))

        updated = []
        for c in order_by_columns:
            if c in ip_cols:
                updated.append(f"{c}_norm")
            else:
                updated.append(c)
        return updated

    def _write_clickhouse_schema(self, schema_df: pd.DataFrame, file_handle: typing.IO) -> None:
        """
        Write the ClickHouse schema definition to the given file handle.
        First writes all column definitions, then writes all index definitions.

        Supported index types:
        - dimension: Set-based index for exact value matches
        - fulltext: Token bloom filter for text search
        - hc: Bloom filter for general purpose filtering
        - minmax: MinMax index for range queries
        - range: MinMax index for range queries (maps to minmax internally)
        - text_search: N-gram Bloom Filter optimized for text search
          Uses ngrambf_v1(3, 256, 2) GRANULARITY 64 for optimal performance
          with syslog and windows event message fields.
          Usage: WHERE field GLOBAL IN INDEX idx_field 'search text'
                 AND field ILIKE '%search text%'

        IP Field Indexing:
        - ip_field (Variant(IPv4, IPv6)) supports any index_type specified by user
        - User is responsible for specifying appropriate index_type (e.g., 'minmax')
        - If _norm materialized column is needed, user must define it in additional_fields
        - Validates no naming conflicts with existing _norm columns via _validate_norm_column_names()

        Args:
            schema_df (pd.DataFrame): The DataFrame containing schema information.
            file_handle (typing.IO): The file handle to write the schema definition to.

        Raises:
            SchemaValidationError: If a column type cannot be matched.
        """
        column_lines = []
        has_default_column = "default" in schema_df.columns

        for _, row in schema_df.iterrows():
            column_name = SchemaUtils.sql_column_fix_name(row["column"])
            column_type = row["type"].lower()
            type_match_df = self.type_map_df.loc[self.type_map_df["type"] == column_type]
            if type_match_df.empty:
                logger.error(f"ClickHouse Schema unable to match type: {column_type}")
                raise SchemaValidationError(f"Invalid type: {column_type}")

            clickhouse_data_type = type_match_df.iloc[0]["clickhouse_type"]
            parts = clickhouse_data_type.split(" CODEC")
            base_type = parts[0].strip()
            codec = "CODEC" + parts[1].strip() if len(parts) > 1 else ""
            column_default = ""
            if (
                has_default_column
                and pd.notnull(row.get("default"))
                and str(row.get("default")).strip() != ""
            ):
                column_default = "DEFAULT " + str(row["default"])
            column_line = f"{column_name} {base_type}{' ' + column_default if column_default else ''}{' ' + codec if codec else ''}".strip()
            column_lines.append(column_line)

        # Generate _norm MATERIALIZED columns for indexed ip_field columns
        norm_columns = []
        for _, row in schema_df.iterrows():
            column_name = SchemaUtils.sql_column_fix_name(row["column"])
            column_type = row["type"].lower()
            index_type = row.get("index_type")

            # determine clickhouse_data_type from type_map (if available)
            type_match_df = self.type_map_df.loc[self.type_map_df["type"] == column_type]
            clickhouse_data_type = ""
            if not type_match_df.empty:
                clickhouse_data_type = type_match_df.iloc[0]["clickhouse_type"]

            # Prefer SchemaUtils helper, but also handle Variant detection when logical type differs
            norm_sql = None
            # If generate_norm_column_sql (from schema_util) would generate it, use that
            generated = generate_norm_column_sql(column_name, column_type, index_type)
            if generated:
                norm_sql = generated
            else:
                # fallback: if mapping shows Variant IPv4/IPv6 and index_type present, generate _norm
                if (
                    self._is_ip_field_variant(column_type, clickhouse_data_type)
                    and pd.notna(index_type)
                    and index_type
                ):
                    norm_sql = self.generate_norm_column_for_ip_field(column_name)

            if norm_sql:
                norm_columns.append(norm_sql)

        SchemaUtils.file_write(file_handle, "\n")
        SchemaUtils.file_write(file_handle, ",\n".join(column_lines))

        # Write _norm columns if present
        if norm_columns:
            SchemaUtils.file_write(file_handle, ",\n")
            SchemaUtils.file_write(file_handle, ",\n".join(norm_columns))

        # Get index definitions
        index_type_df = schema_df[schema_df["index_type"].notna() & (schema_df["index_type"] != "")]
        index_type_length = len(index_type_df)

        if index_type_length > 0:
            SchemaUtils.file_write(file_handle, ",\n")

        if index_type_length > 8:
            logger.warning(
                f"A large number of indexes on a table while improving querying performance "
                f"can impact ingest performance index_type length {index_type_length}"
            )

        # Build index definitions
        index_lines = []
        existing_indexes = set()

        for _, row in index_type_df.iterrows():
            column_name = SchemaUtils.sql_column_fix_name(row["column"])
            column_type = row["type"].lower()
            index_type = row["index_type"]

            # For ip_field columns, create index on _norm column instead of raw Variant
            # Resolve the clickhouse type from type_map to check if it's a Variant
            type_match_df = self.type_map_df.loc[self.type_map_df["type"] == column_type]
            clickhouse_data_type = ""
            if not type_match_df.empty:
                clickhouse_data_type = type_match_df.iloc[0]["clickhouse_type"]

            if self._is_ip_field_variant(column_type, clickhouse_data_type):
                index_column = f"{column_name}_norm"
                index_name = f"idx_{column_name}_norm"
            else:
                index_column = column_name
                index_name = f"idx_{column_name}"

            existing_indexes.add(index_name)

            # Apply index type as specified by user
            if index_type == "dimension":
                index_lines.append(f"INDEX {index_name} {index_column} TYPE set(0) GRANULARITY 4")
            elif index_type == "fulltext":
                index_lines.append(
                    f"INDEX {index_name} {index_column} TYPE tokenbf_v1(8192, 4, 0) GRANULARITY 4"
                )
            elif index_type == "hc":
                index_lines.append(
                    f"INDEX {index_name} {index_column} TYPE bloom_filter GRANULARITY 4"
                )
            elif index_type == "range":
                index_lines.append(f"INDEX {index_name} {index_column} TYPE minmax GRANULARITY 4")
            elif index_type == "minmax":
                index_lines.append(f"INDEX {index_name} {index_column} TYPE minmax GRANULARITY 4")
            elif index_type == "text_search":
                index_lines.append(
                    f"INDEX {index_name} {index_column} TYPE ngrambf_v1(3, 256, 2) GRANULARITY 64"
                )
            # Future: Inverted Index support (currently disabled)
            # elif index_type == 'text_search_inverted':
            #     # Inverted index for text search (requires ClickHouse 23.8 or later)
            #     # Commented out until GA release
            #     # index_lines.append(f'INDEX idx_inverted_{column_name} {column_name} TYPE inverted(8) GRANULARITY 4')

        if index_lines:
            SchemaUtils.file_write(file_handle, ",\n".join(index_lines))
            SchemaUtils.file_write(file_handle, ",\n" + self.PROJECTION_BY_TIMESTAMP.strip())
        else:
            SchemaUtils.file_write(file_handle, self.PROJECTION_BY_TIMESTAMP.strip())

        SchemaUtils.file_write(file_handle, "\n")

    def _write_clickhouse_index_orderby_primarykey(self, source_dfs, index_string, file_handle):
        """
        Write the ORDER BY or PRIMARY KEY clause to the SQL file.
        Always starts with timestamp_load (and cityHash64(timestamp_load) if subsampling is enabled).

        Args:
            source_dfs (list): List of DataFrames containing schema definitions.
            index_string (str): The index clause ('ORDER BY' or 'PRIMARY KEY').
            file_handle: File handle to write to.
        """
        SchemaUtils.file_write(file_handle, f"{index_string} (")

        combined_df = pd.concat(
            [df.dropna(subset=["index_order"]) for df in source_dfs], ignore_index=True
        ).sort_values("index_order")
        combined_df = self.__filter_non_nullable_columns(combined_df, index_string=index_string)

        # Remove timestamp_load from the list as we'll add it first
        combined_df = combined_df[combined_df["column"] != "timestamp_load"]

        # Build final column list with timestamp_load first
        final_columns = []
        if self.use_subsampling_feature:
            final_columns.append("cityHash64(timestamp_load)")
        final_columns.append("timestamp_load")

        # Add remaining columns in order
        column_names = []
        for _, row in combined_df.iterrows():
            column_name = SchemaUtils.sql_column_fix_name(row["column"])
            column_names.append(column_name)

        # Replace ip_field columns with their _norm equivalents
        updated_columns = self.update_order_by_for_ip_fields(column_names, combined_df)
        final_columns.extend(updated_columns)

        SchemaUtils.file_write(file_handle, ", ".join(final_columns) + ")\n")

    def __filter_non_nullable_columns(self, index_df: pd.DataFrame, index_string: str):
        type_map_joined = pd.merge(
            index_df, self.type_map_df, how="left", left_on="type", right_on="type"
        )
        nullable_columns = type_map_joined[
            type_map_joined["clickhouse_type"].str.contains("Nullable")
        ]
        if not nullable_columns.empty:
            for _, row in nullable_columns.iterrows():
                column_name = row["column"]
                clickhouse_type = row["clickhouse_type"]
                logger.warning(
                    f"Column '{column_name}' with nullable ClickHouse type '{clickhouse_type}' are defined in the ORDER BY and PRIMARY KEY in the {self.schema_name} schema."
                )

            logger.error(
                f" Nullable Fields in {self.schema_name} please change the type of these fields or remove them from the {index_string}"
            )

        non_nullable_df = type_map_joined[
            ~type_map_joined["clickhouse_type"].str.contains("Nullable")
        ]

        return non_nullable_df

    def _write_clickhouse_table_create(
        self, schema_name: str, schema_version: str, file_handle: typing.IO
    ) -> None:
        """
        Write the ClickHouse CREATE TABLE statement.

        Args:
            schema_name (str): The name of the schema.
            schema_version (str): The version of the schema.
            file_handle (typing.IO): The file handle to write to.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
        header = f"-- HyperSec DFE {schema_name} v{schema_version} {timestamp}\n"
        create_statement = (
            f"CREATE TABLE IF NOT EXISTS {{{{ org_id }}}}.{schema_name}\n("
            if self.no_cluster_declarations_needed
            else f"CREATE TABLE IF NOT EXISTS {{{{ org_id }}}}.{schema_name} ON CLUSTER {{{{cluster_name}}}}\n("
        )
        SchemaUtils.file_write(file_handle, header + create_statement)

    def _write_clickhouse_table_partitioning(self, file_handle: typing.IO) -> None:
        """
        Write the ClickHouse PARTITION BY statement.

        Args:
            file_handle (typing.IO): The file handle to write to.
        """
        SchemaUtils.file_write(file_handle, self.PARTITION_BY_STATEMENT)

    def _write_clickhouse_table_ttl(self, file_handle: typing.IO) -> None:
        """
        Write the ClickHouse TTL statements.

        Args:
            file_handle (typing.IO): The file handle to write to.
        """
        ttl_statements = f"{self.TTL_STATEMENT.format(ttl=self.ttl)}{self.TTL_STATEMENT_LOAD.format(ttl=self.ttl)}"
        SchemaUtils.file_write(file_handle, ttl_statements)

    def _write_clickhouse_table_settings(self, file_handle: typing.IO) -> None:
        """
        Write the ClickHouse table settings.

        Args:
            file_handle (typing.IO): The file handle to write to.
        """
        SchemaUtils.file_write(file_handle, self.TABLE_SETTINGS)

    def _write_clickhouse_table_engine(self, file_handle: typing.IO) -> None:
        """
        Write the ClickHouse ENGINE statement.

        Args:
            file_handle (typing.IO): The file handle to write to.
        """
        engine_statement = (
            self.ENGINE_SHARED
            if self.use_shared_merge_tree
            else (self.ENGINE_REPLICATED if self.use_replicated_merge_tree else self.ENGINE_MERGE)
        )
        SchemaUtils.file_write(file_handle, engine_statement)

    def _write_json_field_map(self, df: pd.DataFrame, json_field_map_filename: str) -> None:
        """
        Write the JSON field map to a CSV file.

        Args:
            df (pd.DataFrame): The DataFrame containing the field information.
            json_field_map_filename (str): The filename for the JSON field map CSV.

        Raises:
            SchemaExceptions.FileWriteError: If an error occurs during the writing of the CSV file.
        """
        json_field_map_df = df.loc[df["type"] == "json"]

        if not json_field_map_df.empty:
            try:
                json_field_map_df.to_csv(
                    json_field_map_filename,
                    columns=["column"],
                    index=False,
                    header=True,
                )
            except IOError as e:
                logger.error(f"Error writing JSON field map CSV: {e}")
                raise SchemaExceptions.FileWriteError(f"Failed to write JSON field map CSV: {e}")

    def _merge_schema_components(self):
        """
        Load and merge derived-schema and additional fields into the core schema DataFrame.
        """
        derived_schema_df = SchemaUtils.load_derived_schema(
            derived_schema_full_path=self.derived_schema_full_path,
            name=self.schema_name,
            logger=logger,
        )
        self.meta_schema_df = self.meta_schema_df.astype(object)
        self.meta_schema_df = (
            SchemaUtils.apply_derived_schema(
                meta_schema_df=self.meta_schema_df,
                derived_schema_df=derived_schema_df,
                logger=logger,
            )
            if derived_schema_df is not None
            else self.meta_schema_df
        )

        additional_fields_df = SchemaUtils.load_additional_fields(
            additional_fields_full_path=self.additional_fields_full_path,
            name=self.schema_name,
            logger=logger,
        )
        self.meta_schema_df = (
            SchemaUtils.apply_additional_fields(
                meta_schema_df=self.meta_schema_df,
                additional_schema_df=additional_fields_df,
                logger=logger,
            )
            if additional_fields_df is not None
            else self.meta_schema_df
        )

    def build_clickhouse_schema(self):
        """
        Build the ClickHouse schema by loading, applying, and writing the schema definitions.
        """

        self._merge_schema_components()

        ch_schema_filename = os.path.join(self.schema_output_path, self.schema_name + ".sql")
        json_field_map_filename = os.path.join(
            self.schema_output_path, self.schema_name + "_field_map.csv"
        )
        self.common_header_schema_df = self.common_header_schema_df.astype(object)

        complete_schema_df = SchemaUtils.strip_duplicate_config_columns_from_non_common_df(
            self.meta_schema_df,
            self.common_header_schema_df,
            ch_schema_filename,
            logger,
            is_ch_flag=True,
        )

        # Validate _norm columns before building DDL
        self._validate_norm_column_names(complete_schema_df)

        with SchemaUtils.file_open(ch_schema_filename, "w") as file_handle:
            self._write_clickhouse_table_create(self.schema_name, self.version, file_handle)
            self._write_clickhouse_schema(complete_schema_df, file_handle)
            self._write_clickhouse_table_engine(file_handle)
            self._write_clickhouse_table_partitioning(file_handle)
            self._write_clickhouse_index_orderby_primarykey(
                [self.common_header_schema_df, self.meta_schema_df],
                "PRIMARY KEY",
                file_handle,
            )
            self._write_clickhouse_index_orderby_primarykey(
                [self.common_header_schema_df, self.meta_schema_df],
                "ORDER BY",
                file_handle,
            )
            logger.info(f"Sub sampling feature enabled: {self.use_subsampling_feature}")
            if self.use_subsampling_feature:
                SchemaUtils.file_write(file_handle, "SAMPLE BY cityHash64(timestamp_load)\n")
                self._write_clickhouse_table_ttl(file_handle)
            else:
                self._write_clickhouse_table_ttl(file_handle)

            self._write_clickhouse_table_settings(file_handle)

        self._write_json_field_map(self.meta_schema_df, json_field_map_filename)
