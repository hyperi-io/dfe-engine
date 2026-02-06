import numpy as np
import pandas as pd
import pytest

from dfe_engine.schemas.custom_exceptions import SchemaBuilderInvalidVersionError
from dfe_engine.schemas.schema_utils import SchemaUtils
from importlib import resources
from pathlib import Path


pd.set_option('future.no_silent_downcasting', True)


def pytest_generate_tests(metafunc):
    if ("common_header_version" in metafunc.fixturenames):
        marker = metafunc.definition.get_closest_marker("common_header_version")
        
        if (marker):
            path = Path(marker.args[0])
        else:
            schema_utils = SchemaUtils()
            path = schema_utils.common_header_path.parent.parent
        
        versions = [version.name for version in path.iterdir() if version.is_dir()]
        metafunc.parametrize("common_header_version", versions, ids = lambda x: x)
    
    if ("type_maps_version" in metafunc.fixturenames):
        marker = metafunc.definition.get_closest_marker("type_maps_version")

        if (marker):
            path = Path(marker.args[0])
        else:
            schema_utils = SchemaUtils()
            path = schema_utils.type_maps_path.parent.parent
        
        versions = [version.name for version in path.iterdir() if version.is_dir()]
        metafunc.parametrize("type_maps_version", versions, ids = lambda x: x)
    
    if ("meta_schema_name_version" in metafunc.fixturenames):
        marker = metafunc.definition.get_closest_marker("meta_schema_name_version")

        if (marker):
            path = Path(marker.args[0])
        else:
            schema_utils = SchemaUtils()
            path = Path(resources.files(schema_utils.RESOURCES_PACKAGE_PATH)._paths[0]) / "data" / "meta_schemas"
        
        name_versions = [
            (name.name, version.name, path)
            for name in path.iterdir() if name.is_dir()
            for version in name.iterdir() if version.is_dir()
        ]
        metafunc.parametrize("meta_schema_name_version", name_versions, ids = lambda x: f"{x[0]}-{x[1]}")


TEST_SCHEMA_UTILS_GET_VERSION_NO_PATH_INPUT = [
    {
        "name": "test_unnormalized_version",
        "version": "v001.000.000",
        "expected_result": "v001_000_000"
    },
    {
        "name": "test_normalized_version",
        "version": "v001_000_000",
        "expected_result": "v001_000_000"
    },
    {
        "name": "test_invalid_version",
        "version": "invalidversion",
        "raises": {
            "exception": SchemaBuilderInvalidVersionError,
            "message": "Invalid version 'invalidversion'. Please ensure you are using semantic versioning (e.g. v001_000_001)."
        }
    }
]

@pytest.fixture(params = TEST_SCHEMA_UTILS_GET_VERSION_NO_PATH_INPUT, ids = lambda x: x["name"])
def test_schema_utils_get_version_no_path_input(request):
    return request.param


TEST_GET_COMMON_HEADER_DF_INPUT = {
    "v001_000_000": pd.DataFrame([
        {"column": "event_hash", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logoriginal", "type": "text", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.host", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.hostname", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.source", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timestamp", "type": "datetime", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timezone", "type": "string", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.category", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.error", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.site_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.type", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_collector", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_collector_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load", "type": "timestamp", "default": "", "index_order": 0.0, "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_000_001": pd.DataFrame([
        {"column": "event_hash", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logoriginal", "type": "text", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.host", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.hostname", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.source", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timestamp", "type": "datetime", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timezone", "type": "string", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.category", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.error", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.site_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.type", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp", "type": "timestamp", "default": "", "index_order": "", "index_type": "dimension", "ddl_comment": ""},
        {"column": "timestamp_collector", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_collector_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load", "type": "timestamp", "default": "", "index_order": 0.0, "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_001_000": pd.DataFrame([
        {"column": "event_hash", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logoriginal", "type": "text", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.host", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.hostname", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.source", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timestamp", "type": "datetime", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timezone", "type": "string", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.category", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.error", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.site_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.type", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp", "type": "timestamp", "default": "", "index_order": "", "index_type": "dimension", "ddl_comment": ""},
        {"column": "timestamp_collector", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_collector_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load", "type": "timestamp", "default": "now()", "index_order": 0.0, "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_001_001": pd.DataFrame([
        {"column": "event_hash", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logjson", "type": "json", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logoriginal", "type": "text", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.host", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.hostname", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.source", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timestamp", "type": "datetime", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timezone", "type": "string", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.category", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.error", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.site_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.type", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp", "type": "timestamp", "default": "", "index_order": "", "index_type": "dimension", "ddl_comment": ""},
        {"column": "timestamp_collector", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_collector_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load", "type": "timestamp", "default": "now()", "index_order": 0.0, "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_002_000": pd.DataFrame([
        {"column": "event_hash", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logjson", "type": "json", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "logoriginal", "type": "text", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.host", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.hostname", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.source", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timestamp", "type": "datetime", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.collector.timezone", "type": "string", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.category", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.error", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.org_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.site_id", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "tags.event.type", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp", "type": "timestamp", "default": "", "index_order": "", "index_type": "dimension", "ddl_comment": ""},
        {"column": "timestamp_collector", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_collector_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_finalise_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load", "type": "timestamp", "default": "now()", "index_order": 0.0, "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_load_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
        {"column": "timestamp_received_epochms", "type": "int64", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""}
    ]).replace("", np.nan).infer_objects(copy = False)
}

@pytest.fixture()
def test_get_common_header_df_input():
    return TEST_GET_COMMON_HEADER_DF_INPUT


TEST_GET_META_SCHEMAS_DF_INPUT = {
    "logs_alerts": {
        "v001_000_000": pd.DataFrame([
            {"column": "_source", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "_source_event_hash", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "alert_name", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "alert_severity", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "alert_type", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "alert_uid", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "confidence_level", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "confidence_score", "type": "int16", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "detection_time", "type": "timestamp", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "event", "type": "text", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "event_id", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "remediation_steps", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "rule_id", "type": "uuid", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "source_table", "type": "string_fast_lowcardinality", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "tactics", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "tags_str", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""},
            {"column": "techniques", "type": "string_fast", "default": "", "index_order": "", "index_type": "", "ddl_comment": ""}
        ]).replace("", np.nan).infer_objects(copy = False)
    }
}

@pytest.fixture()
def test_get_meta_schemas_df_input():
    return TEST_GET_META_SCHEMAS_DF_INPUT


TEST_GET_TYPE_MAPS_DF_INPUT = {
    "v001_000_000": pd.DataFrame([
        {"type": "boolean", "clickhouse_type": "Nullable(Boolean) CODEC(LZ4)", "clickhouse_type_index": "Boolean CODEC(LZ4)"},
        {"type": "datetime", "clickhouse_type": "Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))"},
        {"type": "float32", "clickhouse_type": "Nullable(Float32) CODEC(ZSTD(1))", "clickhouse_type_index": "Float32 CODEC(ZSTD(1))"},
        {"type": "float64", "clickhouse_type": "Nullable(Float64) CODEC(ZSTD(1))", "clickhouse_type_index": "Float64 CODEC(ZSTD(1))"},
        {"type": "geo_point", "clickhouse_type": "Nullable(Point) CODEC(ZSTD(1))", "clickhouse_type_index": "Point CODEC(ZSTD(1))"},
        {"type": "int128", "clickhouse_type": "Nullable(Int128) CODEC(ZSTD(1))", "clickhouse_type_index": "Int128 CODEC(ZSTD(1))"},
        {"type": "int16", "clickhouse_type": "Nullable(Int16) CODEC(ZSTD(1))", "clickhouse_type_index": "Int16 CODEC(ZSTD(1))"},
        {"type": "int256", "clickhouse_type": "Nullable(Int256) CODEC(ZSTD(1))", "clickhouse_type_index": "Int256 CODEC(ZSTD(1))"},
        {"type": "int32", "clickhouse_type": "Nullable(Int32) CODEC(ZSTD(1))", "clickhouse_type_index": "Int32 CODEC(ZSTD(1))"},
        {"type": "int64", "clickhouse_type": "Nullable(Int64) CODEC(ZSTD(1))", "clickhouse_type_index": "Int64 CODEC(ZSTD(1))"},
        {"type": "int8", "clickhouse_type": "Nullable(Int8) CODEC(ZSTD(1))", "clickhouse_type_index": "Int8 CODEC(ZSTD(1))"},
        {"type": "ipv4", "clickhouse_type": "Nullable(IPv4) CODEC(T64, LZ4)", "clickhouse_type_index": "IPv4 CODEC(LZ4)"},
        {"type": "ipv6", "clickhouse_type": "Nullable(IPv6) CODEC(LZ4)", "clickhouse_type_index": "IPv6 CODEC(LZ4)"},
        {"type": "json", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "map", "clickhouse_type": "Nullable(Map) CODEC(ZSTD(1))", "clickhouse_type_index": "Map CODEC(ZSTD(1))"},
        {"type": "string", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "string_fast", "clickhouse_type": "String CODEC(LZ4)", "clickhouse_type_index": "String CODEC(LZ4)"},
        {"type": "string_fast_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(LZ4)", "clickhouse_type_index": "LowCardinality(String) CODEC(LZ4)"},
        {"type": "string_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(ZSTD(1))", "clickhouse_type_index": "LowCardinality(String) CODEC(ZSTD(1))"},
        {"type": "text", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "timestamp", "clickhouse_type": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)"},
        {"type": "tuple", "clickhouse_type": "Nullable(Tuple) CODEC(ZSTD(1))", "clickhouse_type_index": "Tuple CODEC(ZSTD(1))"},
        {"type": "uuid", "clickhouse_type": "Nullable(UUID) CODEC(ZSTD(1))", "clickhouse_type_index": "UUID CODEC(ZSTD(1))"}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_001_000": pd.DataFrame([
        {"type": "boolean", "clickhouse_type": "Nullable(Boolean) CODEC(LZ4)", "clickhouse_type_index": "Boolean CODEC(LZ4)"},
        {"type": "datetime", "clickhouse_type": "Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))"},
        {"type": "float32", "clickhouse_type": "Nullable(Float32) CODEC(ZSTD(1))", "clickhouse_type_index": "Float32 CODEC(ZSTD(1))"},
        {"type": "float64", "clickhouse_type": "Nullable(Float64) CODEC(ZSTD(1))", "clickhouse_type_index": "Float64 CODEC(ZSTD(1))"},
        {"type": "geo_point", "clickhouse_type": "Nullable(Point) CODEC(ZSTD(1))", "clickhouse_type_index": "Point CODEC(ZSTD(1))"},
        {"type": "int128", "clickhouse_type": "Nullable(Int128) CODEC(ZSTD(1))", "clickhouse_type_index": "Int128 CODEC(ZSTD(1))"},
        {"type": "int16", "clickhouse_type": "Nullable(Int16) CODEC(ZSTD(1))", "clickhouse_type_index": "Int16 CODEC(ZSTD(1))"},
        {"type": "int256", "clickhouse_type": "Nullable(Int256) CODEC(ZSTD(1))", "clickhouse_type_index": "Int256 CODEC(ZSTD(1))"},
        {"type": "int32", "clickhouse_type": "Nullable(Int32) CODEC(ZSTD(1))", "clickhouse_type_index": "Int32 CODEC(ZSTD(1))"},
        {"type": "int64", "clickhouse_type": "Nullable(Int64) CODEC(ZSTD(1))", "clickhouse_type_index": "Int64 CODEC(ZSTD(1))"},
        {"type": "int8", "clickhouse_type": "Nullable(Int8) CODEC(ZSTD(1))", "clickhouse_type_index": "Int8 CODEC(ZSTD(1))"},
        {"type": "ipv4", "clickhouse_type": "Nullable(IPv4) CODEC(T64, LZ4)", "clickhouse_type_index": "IPv4 CODEC(LZ4)"},
        {"type": "ipv6", "clickhouse_type": "Nullable(IPv6) CODEC(LZ4)", "clickhouse_type_index": "IPv6 CODEC(LZ4)"},
        {"type": "json", "clickhouse_type": "JSON", "clickhouse_type_index": "json"},
        {"type": "map", "clickhouse_type": "Nullable(Map) CODEC(ZSTD(1))", "clickhouse_type_index": "Map CODEC(ZSTD(1))"},
        {"type": "string", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "string_fast", "clickhouse_type": "String CODEC(LZ4)", "clickhouse_type_index": "String CODEC(LZ4)"},
        {"type": "string_fast_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(LZ4)", "clickhouse_type_index": "LowCardinality(String) CODEC(LZ4)"},
        {"type": "string_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(ZSTD(1))", "clickhouse_type_index": "LowCardinality(String) CODEC(ZSTD(1))"},
        {"type": "text", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "timestamp", "clickhouse_type": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)"},
        {"type": "tuple", "clickhouse_type": "Nullable(Tuple) CODEC(ZSTD(1))", "clickhouse_type_index": "Tuple CODEC(ZSTD(1))"},
        {"type": "uuid", "clickhouse_type": "Nullable(UUID) CODEC(ZSTD(1))", "clickhouse_type_index": "UUID CODEC(ZSTD(1))"}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_001_001": pd.DataFrame([
        {"type": "boolean", "clickhouse_type": "Nullable(Boolean) CODEC(LZ4)", "clickhouse_type_index": "Boolean CODEC(LZ4)"},
        {"type": "datetime", "clickhouse_type": "Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))"},
        {"type": "float32", "clickhouse_type": "Nullable(Float32) CODEC(ZSTD(1))", "clickhouse_type_index": "Float32 CODEC(ZSTD(1))"},
        {"type": "float64", "clickhouse_type": "Nullable(Float64) CODEC(ZSTD(1))", "clickhouse_type_index": "Float64 CODEC(ZSTD(1))"},
        {"type": "geo_point", "clickhouse_type": "Nullable(Point) CODEC(ZSTD(1))", "clickhouse_type_index": "Point CODEC(ZSTD(1))"},
        {"type": "int128", "clickhouse_type": "Nullable(Int128) CODEC(ZSTD(1))", "clickhouse_type_index": "Int128 CODEC(ZSTD(1))"},
        {"type": "int16", "clickhouse_type": "Nullable(Int16) CODEC(ZSTD(1))", "clickhouse_type_index": "Int16 CODEC(ZSTD(1))"},
        {"type": "int256", "clickhouse_type": "Nullable(Int256) CODEC(ZSTD(1))", "clickhouse_type_index": "Int256 CODEC(ZSTD(1))"},
        {"type": "int32", "clickhouse_type": "Nullable(Int32) CODEC(ZSTD(1))", "clickhouse_type_index": "Int32 CODEC(ZSTD(1))"},
        {"type": "int64", "clickhouse_type": "Nullable(Int64) CODEC(ZSTD(1))", "clickhouse_type_index": "Int64 CODEC(ZSTD(1))"},
        {"type": "int8", "clickhouse_type": "Nullable(Int8) CODEC(ZSTD(1))", "clickhouse_type_index": "Int8 CODEC(ZSTD(1))"},
        {"type": "ip_field", "clickhouse_type": "Variant(IPv4, IPv6) CODEC(LZ4)", "clickhouse_type_index": "Variant(IPv4, IPv6) CODEC(LZ4)"},
        {"type": "ipv4", "clickhouse_type": "Nullable(IPv4) CODEC(LZ4)", "clickhouse_type_index": "IPv4 CODEC(LZ4)"},
        {"type": "ipv6", "clickhouse_type": "Nullable(IPv6) CODEC(LZ4)", "clickhouse_type_index": "IPv6 CODEC(LZ4)"},
        {"type": "json", "clickhouse_type": "JSON", "clickhouse_type_index": "json"},
        {"type": "map", "clickhouse_type": "Nullable(Map) CODEC(ZSTD(1))", "clickhouse_type_index": "Map CODEC(ZSTD(1))"},
        {"type": "string", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "string_fast", "clickhouse_type": "String CODEC(LZ4)", "clickhouse_type_index": "String CODEC(LZ4)"},
        {"type": "string_fast_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(LZ4)", "clickhouse_type_index": "LowCardinality(String) CODEC(LZ4)"},
        {"type": "string_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(ZSTD(1))", "clickhouse_type_index": "LowCardinality(String) CODEC(ZSTD(1))"},
        {"type": "text", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "timestamp", "clickhouse_type": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)"},
        {"type": "tuple", "clickhouse_type": "Nullable(Tuple) CODEC(ZSTD(1))", "clickhouse_type_index": "Tuple CODEC(ZSTD(1))"},
        {"type": "uuid", "clickhouse_type": "Nullable(UUID) CODEC(ZSTD(1))", "clickhouse_type_index": "UUID CODEC(ZSTD(1))"}
    ]).replace("", np.nan).infer_objects(copy = False),
    "v001_002_000": pd.DataFrame([
        {"type": "boolean", "clickhouse_type": "Nullable(Boolean) CODEC(LZ4)", "clickhouse_type_index": "Boolean CODEC(LZ4)"},
        {"type": "datetime", "clickhouse_type": "Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))"},
        {"type": "float32", "clickhouse_type": "Nullable(Float32) CODEC(ZSTD(1))", "clickhouse_type_index": "Float32 CODEC(ZSTD(1))"},
        {"type": "float64", "clickhouse_type": "Nullable(Float64) CODEC(ZSTD(1))", "clickhouse_type_index": "Float64 CODEC(ZSTD(1))"},
        {"type": "geo_point", "clickhouse_type": "Nullable(Point) CODEC(ZSTD(1))", "clickhouse_type_index": "Point CODEC(ZSTD(1))"},
        {"type": "int128", "clickhouse_type": "Nullable(Int128) CODEC(ZSTD(1))", "clickhouse_type_index": "Int128 CODEC(ZSTD(1))"},
        {"type": "int16", "clickhouse_type": "Nullable(Int16) CODEC(ZSTD(1))", "clickhouse_type_index": "Int16 CODEC(ZSTD(1))"},
        {"type": "int256", "clickhouse_type": "Nullable(Int256) CODEC(ZSTD(1))", "clickhouse_type_index": "Int256 CODEC(ZSTD(1))"},
        {"type": "int32", "clickhouse_type": "Nullable(Int32) CODEC(ZSTD(1))", "clickhouse_type_index": "Int32 CODEC(ZSTD(1))"},
        {"type": "int64", "clickhouse_type": "Nullable(Int64) CODEC(ZSTD(1))", "clickhouse_type_index": "Int64 CODEC(ZSTD(1))"},
        {"type": "int8", "clickhouse_type": "Nullable(Int8) CODEC(ZSTD(1))", "clickhouse_type_index": "Int8 CODEC(ZSTD(1))"},
        {"type": "ip_field", "clickhouse_type": "Variant(IPv4, IPv6) CODEC(LZ4)", "clickhouse_type_index": "Variant(IPv4, IPv6) CODEC(LZ4)"},
        {"type": "ipv4", "clickhouse_type": "Nullable(IPv4) CODEC(LZ4)", "clickhouse_type_index": "IPv4 CODEC(LZ4)"},
        {"type": "ipv6", "clickhouse_type": "Nullable(IPv6) CODEC(LZ4)", "clickhouse_type_index": "IPv6 CODEC(LZ4)"},
        {"type": "json", "clickhouse_type": "JSON", "clickhouse_type_index": "json"},
        {"type": "map", "clickhouse_type": "Nullable(Map) CODEC(ZSTD(1))", "clickhouse_type_index": "Map CODEC(ZSTD(1))"},
        {"type": "string", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "string_fast", "clickhouse_type": "String CODEC(LZ4)", "clickhouse_type_index": "String CODEC(LZ4)"},
        {"type": "string_fast_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(LZ4)", "clickhouse_type_index": "LowCardinality(String) CODEC(LZ4)"},
        {"type": "string_lowcardinality", "clickhouse_type": "LowCardinality(String) CODEC(ZSTD(1))", "clickhouse_type_index": "LowCardinality(String) CODEC(ZSTD(1))"},
        {"type": "text", "clickhouse_type": "String CODEC(ZSTD(1))", "clickhouse_type_index": "String CODEC(ZSTD(1))"},
        {"type": "timestamp", "clickhouse_type": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)", "clickhouse_type_index": "DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)"},
        {"type": "tuple", "clickhouse_type": "Nullable(Tuple) CODEC(ZSTD(1))", "clickhouse_type_index": "Tuple CODEC(ZSTD(1))"},
        {"type": "uuid", "clickhouse_type": "Nullable(UUID) CODEC(ZSTD(1))", "clickhouse_type_index": "UUID CODEC(ZSTD(1))"}
    ]).replace("", np.nan).infer_objects(copy = False)
}

@pytest.fixture()
def test_get_type_maps_df_input():
    return TEST_GET_TYPE_MAPS_DF_INPUT