from dfe_engine.schemas.custom_exceptions import SchemaBuilderJSONWarning
from dfe_engine.schemas.schema_utils import SchemaUtils
from hs_pylib import logger


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
        meta_schema_path: str = None,
        type_maps_version: str = None,
        use_json_feature: bool = True
    ):
        schema_utils = SchemaUtils(
            common_header_version = common_header_version,
            meta_schema_path = meta_schema_path,
            type_maps_version = type_maps_version,
            use_json_feature = use_json_feature
        )

        self.common_header_df = schema_utils.get_common_header_df()
        self.meta_schema_df = schema_utils.get_meta_schema_df()
        self.type_maps_df = schema_utils.get_type_maps_df()

        # self.type_map_df = SchemaUtils.load_type_maps(
        #     self.commons_variables_package,
        #     os.path.join(self.common_resource_path, "type_maps.csv"),
        #     SchemaUtils.COLUMNS_TYPE_MAPS,
        #     "type",
        # )