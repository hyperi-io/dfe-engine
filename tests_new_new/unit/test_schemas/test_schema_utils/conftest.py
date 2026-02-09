import numpy as np
import pandas as pd
import pytest
import textwrap

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderError, SchemaBuilderInvalidVersionError
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


TEST_GET_INVALID_COMMON_HEADER_DF_INPUT = [
    {
        "name": "missing_single_column",
        "common_header_version": "v001_000_000",
        "common_header_data": textwrap.dedent("""
            column,type,index_order,index_type,os_order,comment
            ,timestamp,,,,Time the event occurred REQUIRED
            timestamp_collector,timestamp,,,,Time the event was received by a HyperCollector
            timestamp_load,timestamp,0,,,Time the event was loaded into the engine or store REQUIRED
            timestamp_received,timestamp,,,,Time the event was accepted by the receiver
            timestamp_finalise,timestamp,,,,Time the event was finalised in the ingest pipeline
            timestamp_epochms,int64,,,,Epochms time the event occurred 
            timestamp_collector_epochms,int64,,,,Epochms time the event was received by a HyperCollector
            timestamp_load_epochms,int64,,,,Epochms time the event was loaded into the engine or store 
            timestamp_received_epochms,int64,,,,Epochms time the event was accepted by the receiver
            timestamp_finalise_epochms,int64,,,,Epochms time the event was finalised in the ingest pipeline
            event_hash,string_fast,,,0,Unique hash of this event for reference and alerting REQUIRED
            logoriginal,text,,,1,The original unparsed log line
            org_id,string_fast_lowcardinality,,,2,The unique alphanumeric ID of the source organisations
            tags.collector.host,string_fast_lowcardinality,,,7,Host ip and info of the HyperCollector
            tags.collector.hostname,string_fast_lowcardinality,,,8,Hostname of the HyperCollector
            tags.collector.source,string_fast_lowcardinality,,,9,The transport level source
            tags.collector.timestamp,datetime,,,,The time the event was received by the HyperCollector
            tags.collector.timezone,string,,,,The timezone the source HyperCollector is placed
            tags.event.category,string_fast_lowcardinality,,,6,Category of the event selects topic and ingestion routing
            tags.event.org_id,string_fast_lowcardinality,,,3,Source organisation ID maps directly to org_id 
            tags.event.site_id,string_fast_lowcardinality,,,4,Optional site id within the source org_id
            tags.event.type,string_fast_lowcardinality,,,5,Low level type of the message
            tags.event.error,string_fast_lowcardinality,,,,Any parsing or time errors are placed here
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'column' entry identified in '{common_header_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_columns",
        "common_header_version": "v001_000_000",
        "common_header_data": textwrap.dedent("""
            column,type,index_order,index_type,os_order,comment
            ,timestamp,,,,Time the event occurred REQUIRED
            ,timestamp,,,,Time the event was received by a HyperCollector
            timestamp_load,timestamp,0,,,Time the event was loaded into the engine or store REQUIRED
            timestamp_received,timestamp,,,,Time the event was accepted by the receiver
            timestamp_finalise,timestamp,,,,Time the event was finalised in the ingest pipeline
            timestamp_epochms,int64,,,,Epochms time the event occurred 
            timestamp_collector_epochms,int64,,,,Epochms time the event was received by a HyperCollector
            timestamp_load_epochms,int64,,,,Epochms time the event was loaded into the engine or store 
            timestamp_received_epochms,int64,,,,Epochms time the event was accepted by the receiver
            timestamp_finalise_epochms,int64,,,,Epochms time the event was finalised in the ingest pipeline
            event_hash,string_fast,,,0,Unique hash of this event for reference and alerting REQUIRED
            logoriginal,text,,,1,The original unparsed log line
            org_id,string_fast_lowcardinality,,,2,The unique alphanumeric ID of the source organisations
            tags.collector.host,string_fast_lowcardinality,,,7,Host ip and info of the HyperCollector
            tags.collector.hostname,string_fast_lowcardinality,,,8,Hostname of the HyperCollector
            tags.collector.source,string_fast_lowcardinality,,,9,The transport level source
            tags.collector.timestamp,datetime,,,,The time the event was received by the HyperCollector
            tags.collector.timezone,string,,,,The timezone the source HyperCollector is placed
            tags.event.category,string_fast_lowcardinality,,,6,Category of the event selects topic and ingestion routing
            tags.event.org_id,string_fast_lowcardinality,,,3,Source organisation ID maps directly to org_id 
            tags.event.site_id,string_fast_lowcardinality,,,4,Optional site id within the source org_id
            tags.event.type,string_fast_lowcardinality,,,5,Low level type of the message
            tags.event.error,string_fast_lowcardinality,,,,Any parsing or time errors are placed here
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'column' entries identified in '{common_header_file_path}'."
            ]
        }
    },
    {
        "name": "missing_single_type",
        "common_header_version": "v001_000_000",
        "common_header_data": textwrap.dedent("""
            column,type,index_order,index_type,os_order,comment
            timestamp,,,,,Time the event occurred REQUIRED
            timestamp_collector,timestamp,,,,Time the event was received by a HyperCollector
            timestamp_load,timestamp,0,,,Time the event was loaded into the engine or store REQUIRED
            timestamp_received,timestamp,,,,Time the event was accepted by the receiver
            timestamp_finalise,timestamp,,,,Time the event was finalised in the ingest pipeline
            timestamp_epochms,int64,,,,Epochms time the event occurred 
            timestamp_collector_epochms,int64,,,,Epochms time the event was received by a HyperCollector
            timestamp_load_epochms,int64,,,,Epochms time the event was loaded into the engine or store 
            timestamp_received_epochms,int64,,,,Epochms time the event was accepted by the receiver
            timestamp_finalise_epochms,int64,,,,Epochms time the event was finalised in the ingest pipeline
            event_hash,string_fast,,,0,Unique hash of this event for reference and alerting REQUIRED
            logoriginal,text,,,1,The original unparsed log line
            org_id,string_fast_lowcardinality,,,2,The unique alphanumeric ID of the source organisations
            tags.collector.host,string_fast_lowcardinality,,,7,Host ip and info of the HyperCollector
            tags.collector.hostname,string_fast_lowcardinality,,,8,Hostname of the HyperCollector
            tags.collector.source,string_fast_lowcardinality,,,9,The transport level source
            tags.collector.timestamp,datetime,,,,The time the event was received by the HyperCollector
            tags.collector.timezone,string,,,,The timezone the source HyperCollector is placed
            tags.event.category,string_fast_lowcardinality,,,6,Category of the event selects topic and ingestion routing
            tags.event.org_id,string_fast_lowcardinality,,,3,Source organisation ID maps directly to org_id 
            tags.event.site_id,string_fast_lowcardinality,,,4,Optional site id within the source org_id
            tags.event.type,string_fast_lowcardinality,,,5,Low level type of the message
            tags.event.error,string_fast_lowcardinality,,,,Any parsing or time errors are placed here
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Column name 'timestamp' is missing an entry for 'type' in '{common_header_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_types",
        "common_header_version": "v001_000_000",
        "common_header_data": textwrap.dedent("""
            column,type,index_order,index_type,os_order,comment
            timestamp,,,,,Time the event occurred REQUIRED
            timestamp_collector,,,,,Time the event was received by a HyperCollector
            timestamp_load,timestamp,0,,,Time the event was loaded into the engine or store REQUIRED
            timestamp_received,timestamp,,,,Time the event was accepted by the receiver
            timestamp_finalise,timestamp,,,,Time the event was finalised in the ingest pipeline
            timestamp_epochms,int64,,,,Epochms time the event occurred 
            timestamp_collector_epochms,int64,,,,Epochms time the event was received by a HyperCollector
            timestamp_load_epochms,int64,,,,Epochms time the event was loaded into the engine or store 
            timestamp_received_epochms,int64,,,,Epochms time the event was accepted by the receiver
            timestamp_finalise_epochms,int64,,,,Epochms time the event was finalised in the ingest pipeline
            event_hash,string_fast,,,0,Unique hash of this event for reference and alerting REQUIRED
            logoriginal,text,,,1,The original unparsed log line
            org_id,string_fast_lowcardinality,,,2,The unique alphanumeric ID of the source organisations
            tags.collector.host,string_fast_lowcardinality,,,7,Host ip and info of the HyperCollector
            tags.collector.hostname,string_fast_lowcardinality,,,8,Hostname of the HyperCollector
            tags.collector.source,string_fast_lowcardinality,,,9,The transport level source
            tags.collector.timestamp,datetime,,,,The time the event was received by the HyperCollector
            tags.collector.timezone,string,,,,The timezone the source HyperCollector is placed
            tags.event.category,string_fast_lowcardinality,,,6,Category of the event selects topic and ingestion routing
            tags.event.org_id,string_fast_lowcardinality,,,3,Source organisation ID maps directly to org_id 
            tags.event.site_id,string_fast_lowcardinality,,,4,Optional site id within the source org_id
            tags.event.type,string_fast_lowcardinality,,,5,Low level type of the message
            tags.event.error,string_fast_lowcardinality,,,,Any parsing or time errors are placed here
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Column names '[timestamp, timestamp_collector]' are missing an entry for 'type' in '{common_header_file_path}'."
            ]
        }
    },
    {
        "name": "missing_combination_columns_and_types",
        "common_header_version": "v001_000_000",
        "common_header_data": textwrap.dedent("""
            column,type,index_order,index_type,os_order,comment
            timestamp,,,,,Time the event occurred REQUIRED
            ,timestamp,,,,Time the event was received by a HyperCollector
            ,,0,,,Time the event was loaded into the engine or store REQUIRED
            timestamp_received,timestamp,,,,Time the event was accepted by the receiver
            timestamp_finalise,timestamp,,,,Time the event was finalised in the ingest pipeline
            timestamp_epochms,int64,,,,Epochms time the event occurred 
            timestamp_collector_epochms,int64,,,,Epochms time the event was received by a HyperCollector
            timestamp_load_epochms,int64,,,,Epochms time the event was loaded into the engine or store 
            timestamp_received_epochms,int64,,,,Epochms time the event was accepted by the receiver
            timestamp_finalise_epochms,int64,,,,Epochms time the event was finalised in the ingest pipeline
            event_hash,string_fast,,,0,Unique hash of this event for reference and alerting REQUIRED
            logoriginal,text,,,1,The original unparsed log line
            org_id,string_fast_lowcardinality,,,2,The unique alphanumeric ID of the source organisations
            tags.collector.host,string_fast_lowcardinality,,,7,Host ip and info of the HyperCollector
            tags.collector.hostname,string_fast_lowcardinality,,,8,Hostname of the HyperCollector
            tags.collector.source,string_fast_lowcardinality,,,9,The transport level source
            tags.collector.timestamp,datetime,,,,The time the event was received by the HyperCollector
            tags.collector.timezone,string,,,,The timezone the source HyperCollector is placed
            tags.event.category,string_fast_lowcardinality,,,6,Category of the event selects topic and ingestion routing
            tags.event.org_id,string_fast_lowcardinality,,,3,Source organisation ID maps directly to org_id 
            tags.event.site_id,string_fast_lowcardinality,,,4,Optional site id within the source org_id
            tags.event.type,string_fast_lowcardinality,,,5,Low level type of the message
            tags.event.error,string_fast_lowcardinality,,,,Any parsing or time errors are placed here
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'column' entries identified in '{common_header_file_path}'.",
                "Column name 'timestamp' is missing an entry for 'type' in '{common_header_file_path}'."
            ]
        }
    }
]

@pytest.fixture(params = TEST_GET_INVALID_COMMON_HEADER_DF_INPUT, ids = lambda x: x["name"])
def test_get_invalid_common_header_df_input(request):
    return request.param


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


TEST_GET_INVALID_META_SCHEMA_DF_INPUT = [
    {
        "name": "missing_single_column",
        "meta_schema_name": "logs_alerts",
        "meta_schema_version": "v001_000_000",
        "meta_schema_data": textwrap.dedent("""
            column,type,index_order,os_order,comment
            ,string_fast,,,"Generate UUID for this specific alert that has occurred"
            tags_str,string_fast,,,"keywords that describe the event"
            _source_event_hash,string_fast,,,"Extracted copy of event that triggered alert"
            _source,string_fast_lowcardinality,,,"Add Rule Name"
            alert_name,string_fast,,,"Name of the alert / Display Name"
            alert_severity,string_fast,,,"enum Unassigned / Informational / Low / Medium / High / Critical"
            alert_type,string_fast,,,"scheduled alert / adhoc alert"
            confidence_level,string_fast_lowcardinality,,,"likelihood this is a real alert vs false positive"
            confidence_score,int16,,,"int representation of above"
            detection_time,timestamp,,,"Time alert was generated by our detection system"
            event_id,string_fast,,,"Original event ID"
            event,text,,,"Original event as JSON event string"
            remediation_steps,string_fast,,,"Triggers for soar"
            rule_id,uuid,,,"UUID of rule which triggered alert"
            source_table,string_fast_lowcardinality,,,"source table the alert was generated from"
            tactics,string_fast,,,"comma sep list of mtire att&ck tactics"
            techniques,string_fast,,,"comma sep list of mtire att&ck techniques"
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'column' entry identified in '{meta_schema_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_columns",
        "meta_schema_name": "logs_alerts",
        "meta_schema_version": "v001_000_000",
        "meta_schema_data": textwrap.dedent("""
            column,type,index_order,os_order,comment
            ,string_fast,,,"Generate UUID for this specific alert that has occurred"
            ,string_fast,,,"keywords that describe the event"
            _source_event_hash,string_fast,,,"Extracted copy of event that triggered alert"
            _source,string_fast_lowcardinality,,,"Add Rule Name"
            alert_name,string_fast,,,"Name of the alert / Display Name"
            alert_severity,string_fast,,,"enum Unassigned / Informational / Low / Medium / High / Critical"
            alert_type,string_fast,,,"scheduled alert / adhoc alert"
            confidence_level,string_fast_lowcardinality,,,"likelihood this is a real alert vs false positive"
            confidence_score,int16,,,"int representation of above"
            detection_time,timestamp,,,"Time alert was generated by our detection system"
            event_id,string_fast,,,"Original event ID"
            event,text,,,"Original event as JSON event string"
            remediation_steps,string_fast,,,"Triggers for soar"
            rule_id,uuid,,,"UUID of rule which triggered alert"
            source_table,string_fast_lowcardinality,,,"source table the alert was generated from"
            tactics,string_fast,,,"comma sep list of mtire att&ck tactics"
            techniques,string_fast,,,"comma sep list of mtire att&ck techniques"
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'column' entries identified in '{meta_schema_file_path}'."
            ]
        }
    },
    {
        "name": "missing_single_type",
        "meta_schema_name": "logs_alerts",
        "meta_schema_version": "v001_000_000",
        "meta_schema_data": textwrap.dedent("""
            column,type,index_order,os_order,comment
            alert_uid,,,,"Generate UUID for this specific alert that has occurred"
            tags_str,string_fast,,,"keywords that describe the event"
            _source_event_hash,string_fast,,,"Extracted copy of event that triggered alert"
            _source,string_fast_lowcardinality,,,"Add Rule Name"
            alert_name,string_fast,,,"Name of the alert / Display Name"
            alert_severity,string_fast,,,"enum Unassigned / Informational / Low / Medium / High / Critical"
            alert_type,string_fast,,,"scheduled alert / adhoc alert"
            confidence_level,string_fast_lowcardinality,,,"likelihood this is a real alert vs false positive"
            confidence_score,int16,,,"int representation of above"
            detection_time,timestamp,,,"Time alert was generated by our detection system"
            event_id,string_fast,,,"Original event ID"
            event,text,,,"Original event as JSON event string"
            remediation_steps,string_fast,,,"Triggers for soar"
            rule_id,uuid,,,"UUID of rule which triggered alert"
            source_table,string_fast_lowcardinality,,,"source table the alert was generated from"
            tactics,string_fast,,,"comma sep list of mtire att&ck tactics"
            techniques,string_fast,,,"comma sep list of mtire att&ck techniques"
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Column name 'alert_uid' is missing an entry for 'type' in '{meta_schema_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_types",
        "meta_schema_name": "logs_alerts",
        "meta_schema_version": "v001_000_000",
        "meta_schema_data": textwrap.dedent("""
            column,type,index_order,os_order,comment
            alert_uid,,,,"Generate UUID for this specific alert that has occurred"
            tags_str,,,,"keywords that describe the event"
            _source_event_hash,string_fast,,,"Extracted copy of event that triggered alert"
            _source,string_fast_lowcardinality,,,"Add Rule Name"
            alert_name,string_fast,,,"Name of the alert / Display Name"
            alert_severity,string_fast,,,"enum Unassigned / Informational / Low / Medium / High / Critical"
            alert_type,string_fast,,,"scheduled alert / adhoc alert"
            confidence_level,string_fast_lowcardinality,,,"likelihood this is a real alert vs false positive"
            confidence_score,int16,,,"int representation of above"
            detection_time,timestamp,,,"Time alert was generated by our detection system"
            event_id,string_fast,,,"Original event ID"
            event,text,,,"Original event as JSON event string"
            remediation_steps,string_fast,,,"Triggers for soar"
            rule_id,uuid,,,"UUID of rule which triggered alert"
            source_table,string_fast_lowcardinality,,,"source table the alert was generated from"
            tactics,string_fast,,,"comma sep list of mtire att&ck tactics"
            techniques,string_fast,,,"comma sep list of mtire att&ck techniques"
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Column names '[alert_uid, tags_str]' are missing an entry for 'type' in '{meta_schema_file_path}'."
            ]
        }
    },
    {
        "name": "missing_combination_columns_and_types",
        "meta_schema_name": "logs_alerts",
        "meta_schema_version": "v001_000_000",
        "meta_schema_data": textwrap.dedent("""
            column,type,index_order,os_order,comment
            ,string_fast,,,"Generate UUID for this specific alert that has occurred"
            tags_str,,,,"keywords that describe the event"
            ,,,,"Extracted copy of event that triggered alert"
            _source,string_fast_lowcardinality,,,"Add Rule Name"
            alert_name,string_fast,,,"Name of the alert / Display Name"
            alert_severity,string_fast,,,"enum Unassigned / Informational / Low / Medium / High / Critical"
            alert_type,string_fast,,,"scheduled alert / adhoc alert"
            confidence_level,string_fast_lowcardinality,,,"likelihood this is a real alert vs false positive"
            confidence_score,int16,,,"int representation of above"
            detection_time,timestamp,,,"Time alert was generated by our detection system"
            event_id,string_fast,,,"Original event ID"
            event,text,,,"Original event as JSON event string"
            remediation_steps,string_fast,,,"Triggers for soar"
            rule_id,uuid,,,"UUID of rule which triggered alert"
            source_table,string_fast_lowcardinality,,,"source table the alert was generated from"
            tactics,string_fast,,,"comma sep list of mtire att&ck tactics"
            techniques,string_fast,,,"comma sep list of mtire att&ck techniques"
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'column' entries identified in '{meta_schema_file_path}'.",
                "Column name 'tags_str' is missing an entry for 'type' in '{meta_schema_file_path}'."
            ]
        }
    }
]

@pytest.fixture(params = TEST_GET_INVALID_META_SCHEMA_DF_INPUT, ids = lambda x: x["name"])
def test_get_invalid_meta_schema_df_input(request):
    return request.param


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


TEST_GET_TYPE_MAPS_NO_STRING_DF_INPUT = {
    "type_maps_version": "v001_000_000",
    "type_maps_data": textwrap.dedent("""
        type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
        string_fast,String CODEC(LZ4),String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
        string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
        string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
        text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
        json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
        int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
        int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
        int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
        int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
        int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
        int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
        float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
        float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
        boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
        timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
        datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
        ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
        ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
        geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
        tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
        map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
        uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
    """)
}

@pytest.fixture()
def test_get_type_maps_no_string_df_input():
    return TEST_GET_TYPE_MAPS_NO_STRING_DF_INPUT


TEST_GET_INVALID_TYPE_MAPS_DF_INPUT = [
    {
        "name": "missing_single_type",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            ,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            string_fast,String CODEC(LZ4),String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'type' entry identified in '{type_maps_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_types",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            ,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            ,String CODEC(LZ4),String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'type' entries identified in '{type_maps_file_path}'."
            ]
        }
    },
    {
        "name": "missing_single_clickhouse_type",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            string,,String CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            string_fast,String CODEC(LZ4),String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Type name 'string' is missing an entry for 'clickhouse_type' in '{type_maps_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_clickhouse_types",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            string,,String CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            string_fast,,String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Type names '[string, string_fast]' are missing an entry for 'clickhouse_type' in '{type_maps_file_path}'."
            ]
        }
    },
    {
        "name": "missing_single_clickhouse_type_index",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            string,String CODEC(ZSTD(1)),,"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            string_fast,String CODEC(LZ4),String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Type name 'string' is missing an entry for 'clickhouse_type_index' in '{type_maps_file_path}'."
            ]
        }
    },
    {
        "name": "missing_multiple_clickhouse_types",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            string,String CODEC(ZSTD(1)),,"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            string_fast,String CODEC(LZ4),,"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),LowCardinality(String) CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            string_fast_lowcardinality,LowCardinality(String) CODEC(LZ4),LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            text,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Type names '[string, string_fast]' are missing an entry for 'clickhouse_type_index' in '{type_maps_file_path}'."
            ]
        }
    },
    {
        "name": "missing_types_missing_clickhouse_types_missing_clickhouse_type_indexes",
        "type_maps_version": "v001_000_000",
        "type_maps_data": textwrap.dedent("""
            type,clickhouse_type,clickhouse_type_index,opensearch_type,comment
            ,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",
            string_fast,,String CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried strings that can accept greater disk storage consumption
            string_lowcardinality,LowCardinality(String) CODEC(ZSTD(1)),,"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Low cardinality strings
            ,,LowCardinality(String) CODEC(LZ4),"{""type"":""keyword"",""ignore_above"":1024,""normalizer"": ""lowercase_normalizer""}",Heavily queried low cardinality strings that can accept greater disk storage consumption
            ,String CODEC(ZSTD(1)),,"{""type"":""text"",""norms"":false}",Larger strings and text fields
            json,,,"{""type"": ""object""}",Move to native JSON type once it is GA in ClickHouse
            int8,Nullable(Int8) CODEC(ZSTD(1)),Int8 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int16,Nullable(Int16) CODEC(ZSTD(1)),Int16 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int32,Nullable(Int32) CODEC(ZSTD(1)),Int32 CODEC(ZSTD(1)),"{""type"": ""integer""}",
            int64,Nullable(Int64) CODEC(ZSTD(1)),Int64 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int128,Nullable(Int128) CODEC(ZSTD(1)),Int128 CODEC(ZSTD(1)),"{""type"": ""long""}",
            int256,Nullable(Int256) CODEC(ZSTD(1)),Int256 CODEC(ZSTD(1)),"{""type"": ""long""}",
            float32,Nullable(Float32) CODEC(ZSTD(1)),Float32 CODEC(ZSTD(1)),"{""type"": ""float""}",
            float64,Nullable(Float64) CODEC(ZSTD(1)),Float64 CODEC(ZSTD(1)),"{""type"": ""double""}",
            boolean,Nullable(Boolean) CODEC(LZ4),Boolean CODEC(LZ4),"{""type"": ""boolean""}",
            timestamp,"DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","DateTime64(3,'UTC') CODEC(DoubleDelta, LZ4)","{""type"": ""date""}","Timeseries increasing timestamp (timestamp, timestamp_load) REQUIRED NOT NULLABLE"
            datetime,"Nullable(DateTime64(3,'UTC')) CODEC(DoubleDelta, ZSTD(1))","DateTime64(3,'UTC') CODEC(DoubleDelta, ZSTD(1))","{""type"": ""date""}",All other dates and timestamps nullable
            ipv4,"Nullable(IPv4) CODEC(T64, LZ4)",IPv4 CODEC(LZ4),"{""type"": ""ip""}",
            ipv6,Nullable(IPv6) CODEC(LZ4),IPv6 CODEC(LZ4),"{""type"": ""ip""}",
            geo_point,Nullable(Point) CODEC(ZSTD(1)),Point CODEC(ZSTD(1)),"{""type"": ""geo_point""}",
            tuple,Nullable(Tuple) CODEC(ZSTD(1)),Tuple CODEC(ZSTD(1)),"{""type"": ""object""}",
            map,Nullable(Map) CODEC(ZSTD(1)),Map CODEC(ZSTD(1)),"{""type"": ""object""}",
            uuid,Nullable(UUID) CODEC(ZSTD(1)),UUID CODEC(ZSTD(1)),"{""type"":""keyword"",""ignore_above"":68,""normalizer"": ""lowercase_normalizer""}",Support all GUID types on OpenSearch
        """),
        "raises": {
            "exception": SchemaError,
            "messages": [
                "Empty 'type' entries identified in '{type_maps_file_path}'.",
                "Type names '[json, string_fast]' are missing an entry for 'clickhouse_type' in '{type_maps_file_path}'.",
                "Type names '[json, string_lowcardinality]' are missing an entry for 'clickhouse_type_index' in '{type_maps_file_path}'."
            ]
        }
    }
]

@pytest.fixture(params = TEST_GET_INVALID_TYPE_MAPS_DF_INPUT, ids = lambda x: x["name"])
def test_get_invalid_type_maps_df_input(request):
    return request.param