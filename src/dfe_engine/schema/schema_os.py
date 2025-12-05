import json
import os
from pathlib import Path
import pandas as pd
from typing import List, Optional
from datetime import datetime
import logging
from .schema_util import (
    SchemaUtils,
    SchemaValidationError,
)


class OpenSearchTemplate:
    """
    A class to generate OpenSearch templates from Elasticsearch templates or build them from CSV definitions.

    Attributes:
        name (str): Name of the schema/template.
        version (Optional[str]): Version of the schema/template.
        meta_schema_file_path (str): Path to the meta schema CSV.
        common_resource_path (str): Path to the common headers CSV.
        derived_schema_full_path (Optional[str]): Path to the sub-schema CSV.
        additional_fields_full_path (Optional[str]): Path to the additional fields CSV.
        dfe_output_path (str): Output directory for generated templates.
        source_template_type: str = 'elastic' | 'xde_custom', elastic == json and xde_custom == csv.
        logger (Optional[logging.Logger]): Logger instance.
    """

    def __init__(
        self,
        name: str,
        version: Optional[str],
        meta_schema_file_path: Path,
        common_resource_path: str,
        derived_schema_full_path: str = None,
        additional_fields_full_path: str = None,
        dfe_output_path: str = os.path.join(os.getcwd(), "dfeoutput"),
        source_template_type: str = "xde_custom",
        logger: Optional[logging.Logger] = None,
    ):
        # Schema identification
        self.schema_name = name
        self.version = version

        # Paths and packages
        self.meta_schema_file_path = meta_schema_file_path
        self.common_resource_path = common_resource_path
        self.derived_schema_full_path = derived_schema_full_path
        self.additional_fields_full_path = additional_fields_full_path
        self.commons_variables_package = "dfecli.resources"

        # Logger setup
        self.logger = logger if logger else logging.getLogger(__name__)

        # Internal state
        self.schema_output_path = os.path.join(dfe_output_path, self.schema_name)
        self.os_index_filename = os.path.join(
            self.schema_output_path, self.schema_name + "_opensearch_template.json"
        )
        self.os_index_filename_cm = os.path.join(
            self.schema_output_path, self.schema_name + "_opensearch_template_cm.json"
        )
        self.source_template_type = source_template_type
        self.template_extension = (
            "csv" if source_template_type == "xde_custom" else "json"
        )

        # Load type map
        self.type_map_df = SchemaUtils.load_type_maps(
            self.commons_variables_package,
            os.path.join(self.common_resource_path, "type_maps.csv"),
            SchemaUtils.COLUMNS_TYPE_MAPS,
            "type",
        )

        self.common_header_schema_df = SchemaUtils.load_schema_from_resource_package(
            self.commons_variables_package,
            os.path.join(self.common_resource_path, "common_header.csv"),
        )

        self.meta_schema_df = SchemaUtils.load_schema_from_directory(
            self.meta_schema_file_path
        )

    def _merge_schema_components(self):
        """
        Load and merge sub-schema and additional fields into the core schema DataFrame.
        """
        derived_schema_df = SchemaUtils.load_derived_schema(
            derived_schema_full_path=self.derived_schema_full_path,
            name=self.schema_name,
            logger=self.logger,
        )
        self.meta_schema_df = (
            SchemaUtils.apply_derived_schema(
                meta_schema_df=self.meta_schema_df,
                derived_schema_df=derived_schema_df,
                logger=self.logger,
            )
            if derived_schema_df is not None
            else self.meta_schema_df
        )

        additional_fields_df = SchemaUtils.load_additional_fields(
            additional_fields_full_path=self.additional_fields_full_path,
            name=self.schema_name,
            logger=self.logger,
        )
        self.meta_schema_df = (
            SchemaUtils.apply_additional_fields(
                meta_schema_df=self.meta_schema_df,
                additional_schema_df=additional_fields_df,
                logger=self.logger,
            )
            if additional_fields_df is not None
            else self.meta_schema_df
        )

        self.cleansed_meta_schema_df = (
            SchemaUtils.strip_duplicate_config_columns_from_non_common_df(
                self.meta_schema_df,
                self.common_header_schema_df,
                self.schema_name,
                self.logger,
                is_ch_flag=False,
            )
        )

    def _set_default_os_template(self) -> None:
        """
        Set the default OpenSearch template structure in memory.
        """
        pattern = self.schema_name.replace("_", "-") + "-*"
        priority = 100 + (len(pattern.rstrip("-").split("-")) * 10)

        self.os_template = {
            "_meta": {
                "description": "HyperSec "
                + self.schema_name
                + " OpenSearch ISM log streaming template. v"
                + self.version
                + " "
                + datetime.now().strftime("%Y-%m-%d-%H:%M:%S")
            },
            "composed_of": ["hypersec-log-component-template"],
            "priority": str(priority),
            "data_stream": {"timestamp_field": {"name": "@timestamp"}},
            "index_patterns": [pattern, f".ds-{pattern}"],
            "template": {
                "settings": {
                    "mapping.total_fields.limit": 10000,
                    "index": {
                        "query": {
                            "default_field": [
                                "event_hash",
                                "logoriginal",
                                "org_id",
                                "tags.event.org_id",
                                "tags.event.site_id",
                                "tags.event.type",
                                "tags.event.category",
                                "tags.collector.host",
                                "tags.collector.hostname",
                                "tags.collector.source",
                                "event_hash",
                                "message",
                                "logoriginal",
                            ]
                        }
                    },
                },
                "mappings": {"date_detection": False, "properties": {}},
            },
        }

    @staticmethod
    def __is_json(json_in_filename):
        try:
            json.loads(json_in_filename)
        except ValueError:
            return False
        return True

    def _set_os_template_val(self, key: str, value: str) -> None:
        """
        Set a value in the OpenSearch template.

        Args:
            key (str): The key path in dot notation.
            value (str): The value to set.
        """
        keys = key.split(".")
        ref = self.os_template

        for k in keys[:-1]:
            ref = ref.setdefault(k, {})

        if isinstance(value, str) and self.__is_json(value):
            ref[keys[-1]] = json.loads(value)
        else:
            ref[keys[-1]] = value

    def _set_schema_os_template(self, source_dfs: List[pd.DataFrame]):
        """
        Set up OpenSearch template from source dataframes.
        Parent fields should only have properties, no type.

        Args:
            source_dfs (list of pd.DataFrame): Source DataFrames to create OpenSearch template.
        """
        # First build a map of all fields and their children
        field_children = {}
        for df in source_dfs:
            for _, row in df.iterrows():
                field_name = (
                    "@timestamp" if row["column"] == "timestamp" else row["column"]
                )
                parts = field_name.split(".")
                for i in range(len(parts) - 1):
                    parent = ".".join(parts[: i + 1])
                    field_children[parent] = field_children.get(parent, set())
                    field_children[parent].add(".".join(parts[: i + 2]))

        # Now process each field
        properties = {}
        for df in source_dfs:
            for _, row in df.iterrows():
                column_type = row["type"]
                field_name = (
                    "@timestamp" if row["column"] == "timestamp" else row["column"]
                )

                # Get field type from type map
                type_match = self.type_map_df.loc[
                    self.type_map_df["type"] == column_type
                ]
                if type_match.empty:
                    raise SchemaValidationError(
                        f"Type '{column_type}' not recognized in the type map."
                    )

                # Get field definition
                opensearch_data_type = type_match.iloc[0]["opensearch_type"]
                field_type = (
                    json.loads(opensearch_data_type)
                    if isinstance(opensearch_data_type, str)
                    and self.__is_json(opensearch_data_type)
                    else {"type": opensearch_data_type}
                    if isinstance(opensearch_data_type, str)
                    else opensearch_data_type.copy()
                )

                # Rule: Fields with properties or children get only properties
                if field_name in field_children or "properties" in field_type:
                    field_type = {"properties": field_type.get("properties", {})}

                # Set the field in the properties structure
                current = properties
                parts = field_name.split(".")
                for i, part in enumerate(parts[:-1]):
                    parent_path = ".".join(parts[: i + 1])
                    if parent_path in field_children:
                        if part not in current:
                            current[part] = {"properties": {}}
                        current = current[part]["properties"]
                    else:
                        if part not in current:
                            current[part] = field_type
                        current = current[part]
                current[parts[-1]] = field_type

        # Set the final properties
        self.os_template["template"]["mappings"]["properties"] = properties

    def _set_default_query(self, source_dfs: List[pd.DataFrame]) -> None:
        """
        Set the default query fields for the OpenSearch template.

        Args:
            source_dfs (list of pd.DataFrame): Source DataFrames to create the default query.
        """
        default_query = []
        for df in source_dfs:
            if SchemaUtils.df_col_not_empty(df, "os_order"):
                order_col = "os_order"
            else:
                order_col = "index_order"
                self.logger.info(
                    "OpenSearch order not supplied (os_order), using index_order"
                )

            df = df.dropna(subset=[order_col])
            df = df[df[order_col] != ""]
            df = df.sort_values(order_col)
            for _, row in df.iterrows():
                default_query.append(row["column"])
        # Append standard header fields and always search 'message' and 'logoriginal' at the end
        default_query.extend(["event_hash", "message", "logoriginal"])
        self._set_os_template_val(
            "template.settings.index.query.default_field", default_query
        )

    def build_opensearch_template(self) -> None:
        """
        Build the OpenSearch template by loading, applying, and writing the schema definitions.
        """
        SchemaUtils.create_path(self.schema_output_path)

        self._set_default_os_template()

        self._merge_schema_components()

        self._set_schema_os_template(
            [self.common_header_schema_df, self.cleansed_meta_schema_df]
        )

        self._set_default_query([self.cleansed_meta_schema_df])

        self.logger.info(
            f"Writing OpenSearch index template to {self.os_index_filename}"
        )
        with open(self.os_index_filename, "w") as os_schema_file:
            json.dump(self.os_template, os_schema_file, indent=2)

        self.logger.info(f"OpenSearch index template {self.os_index_filename} ready")
