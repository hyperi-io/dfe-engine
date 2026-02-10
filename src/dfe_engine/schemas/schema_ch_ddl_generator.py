from dfe_engine.schemas.custom_exceptions import SchemaBuilderError, SchemaBuilderMetaSchemaError
from dfe_engine.schemas.schema_utils import SchemaUtils
from hs_pylib import logger
from pathlib import Path


class SchemaCHDDLGenerator:

    ENGINE_STATEMENT = "ENGINE = {engine}"                                                  # DEFAULT TO merge
    PARTITION_BY_STATEMENT = "PARTITION BY toYYYYMMDD({field})"                             # DEFAULT TO timestamp_load
    PROJECTION_STATEMENT = "PROJECTION {field}_projection (SELECT * ORDER BY {field})"      # DEFAULT TO timestamp
    SETTINGS_STATEMENT = "SETTINGS{settings_string}"
    TTL_STATEMENT = "TTL {ttl_sub_statements}"
    TTL_SUB_STATEMENT = "{field} + INTERVAL {ttl} DAY DELETE WHERE {field} >= 0"
    
    ENGINE_TYPES = [
        {
            "type": "merge",
            "engine": "MergeTree()"
        },
        {
            "type": "replicated",
            "engine": "ReplicatedMergeTree()"
        },
        {
            "type": "shared",
            "engine": "SharedMergeTree()"
        }
    ]

    SETTINGS = [
        {
            "name": "index_granularity",
            "value": "2048"
        },
        {
            "name": "ttl_only_drop_parts",
            "value": "1"
        }
    ]

    def __init__(
        self,
        common_header_version: str = None,
        type_maps_version: str = None,
        use_json_feature: bool = True
    ):
        self.schema_utils = SchemaUtils(
            common_header_version = common_header_version,
            type_maps_version = type_maps_version,
            use_json_feature = use_json_feature
        )

        try:
            self.common_header_df = self.schema_utils.get_common_header_df()
            self.type_maps_df = self.schema_utils.get_type_maps_df()
        
        except SchemaBuilderMetaSchemaError:
            raise
        
        except SchemaBuilderError:
            raise
    

    def build_ddl(
        self,
        meta_schema_path: Path,
        derived_schema_path: Path
    ) -> str:
        fields_df = self.schema_utils.create_combined_df(
            common_header_df = self.common_header_df,
            derived_schema_path = derived_schema_path,
            meta_schema_path = meta_schema_path,
            type_maps_df = self.type_maps_df
        )