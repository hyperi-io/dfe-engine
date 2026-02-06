import csv
import io
import numpy as np
import pandas as pd
import pytest
import textwrap

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderError, SchemaBuilderDirNotFoundError
from dfe_engine.schemas.schema_utils import SchemaUtils
from pathlib import Path

from ..templates.derived_schema import LOGS_TEST_DERIVED_001_000_000, LOGS_TEST_DERIVED_001_000_001
from ..templates.dfe_package import DFE_PKG_SINGLE_SCHEMA
from ..templates.meta_schema import LOGS_TEST_META
from ..test_schema.test_schema import init_schema


@pytest.fixture
def init_schema_utils():
    
    def _init_(
        common_header_path = None,
        common_header_version = None,
        meta_schema_path = None,
        type_maps_path = None,
        type_maps_version = None,
        use_json_feature = False
    ):
        schema_utils = SchemaUtils(
            common_header_path = common_header_path,
            common_header_version = common_header_version,
            meta_schema_path = meta_schema_path,
            type_maps_path = type_maps_path,
            type_maps_version = type_maps_version,
            use_json_feature = use_json_feature
        )
        return schema_utils
    
    return _init_


def test_get_version_no_path(init_schema_utils, init_schema, test_schema_utils_get_version_no_path_input):
    name = test_schema_utils_get_version_no_path_input["name"]
    version = test_schema_utils_get_version_no_path_input["version"]
    expected_result = test_schema_utils_get_version_no_path_input.get("expected_result", None)
    raises = test_schema_utils_get_version_no_path_input.get("raises", None)

    init_schema(
        schema_name = name,
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_000,
        meta_schema_data = LOGS_TEST_META
    )
    
    schema_utils = init_schema_utils()

    if (raises):
        with pytest.raises(raises["exception"]) as exc_info:
            schema_utils._get_version(
                version = version
            )
        assert (raises["message"] in str(exc_info.value))
        return

    identified_version = schema_utils._get_version(
        version = version
    )

    assert(expected_result == identified_version)


def test_get_version_one_version_path(init_schema_utils, init_schema):
    name = "test_get_version_one_version_path"
    expected_version = "v001_000_000"

    latest_schema = init_schema(
        schema_name = name,
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_000,
        meta_schema_data = LOGS_TEST_META
    )
    
    schema_utils = init_schema_utils()

    identified_version = schema_utils._get_version(
        path = Path(latest_schema.info["derived_schema"]["path"]).parent.parent
    )

    assert(expected_version == identified_version)


def test_get_version_two_version_path(init_schema_utils, init_schema):
    name = "test_get_version_one_version_path"
    expected_version = "v001_000_001"

    init_schema(
        schema_name = name,
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_000,
        meta_schema_data = LOGS_TEST_META
    )

    latest_schema = init_schema(
        schema_name = name,
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_001,
        meta_schema_data = LOGS_TEST_META
    )
    
    schema_utils = init_schema_utils()

    identified_version = schema_utils._get_version(
        path = Path(latest_schema.info["derived_schema"]["path"]).parent.parent
    )

    assert(expected_version == identified_version)


def test_get_version_invalid_path(init_schema_utils, init_schema):
    name = "test_get_version_one_version_path"
    expected_message = "The directory '{directory}' could not be found."

    init_schema(
        schema_name = name,
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_000,
        meta_schema_data = LOGS_TEST_META
    )

    latest_schema = init_schema(
        schema_name = name,
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_001,
        meta_schema_data = LOGS_TEST_META
    )
    
    schema_utils = init_schema_utils()

    with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
        schema_utils._get_version(
            path = Path(latest_schema.info["derived_schema"]["path"]).parent.parent / "non-existant"
        )
    assert (expected_message.format(directory = Path(latest_schema.info["derived_schema"]["path"]).parent.parent / "non-existant") in str(exc_info.value))


def test_csv_to_dataframe(tmp_path, init_schema_utils):
    csv_file_path = Path(tmp_path) / "test_csv_to_dataframe.csv"
    csv_data = textwrap.dedent("""
        column,type,default,index_order,index_type,comment
        test_field_1,string,,,,
        test_field_2,string,,,,
        test_field_3,string,,,,
        test_field_4,string,,,,
        test_field_5,string,,,,
        test_field_6,string,,,,
        test_field_7,string,,,,
        test_field_8,string,,,,
        test_field_9,string,,,,
        test_field_10,string,,,,
    """)
    expected_dataframe = pd.DataFrame({
        "column": ["test_field_1", "test_field_2", "test_field_3", "test_field_4", "test_field_5", "test_field_6", "test_field_7", "test_field_8", "test_field_9", "test_field_10"],
        "type": ["string", "string", "string", "string", "string", "string", "string", "string", "string", "string"],
        "default": ["", "", "", "", "", "", "", "", "", ""],
        "index_order": ["", "", "", "", "", "", "", "", "", ""],
        "index_type": ["", "", "", "", "", "", "", "", "", ""],
        "comment": ["", "", "", "", "", "", "", "", "", ""]
    }).replace("", np.nan)

    with open(csv_file_path, "w") as file:
        reader = csv.reader(io.StringIO(csv_data))
        writer = csv.writer(file)
        writer.writerows(reader)
    
    schema_utils = init_schema_utils()

    dataframe = schema_utils._csv_to_dataframe(csv_file_path)

    pd.testing.assert_frame_equal(expected_dataframe, dataframe)
    assert(expected_dataframe.equals(dataframe))


def test_get_common_header_df(init_schema_utils, common_header_version, test_get_common_header_df_input):
    version = common_header_version
    expected_dataframe = test_get_common_header_df_input[version]

    schema_utils = init_schema_utils(
        common_header_version = version
    )

    common_header_df = schema_utils.get_common_header_df()

    pd.testing.assert_frame_equal(expected_dataframe, common_header_df)
    assert(expected_dataframe.equals(common_header_df))


def test_get_invalid_common_header_df(init_schema_utils):
    invalid_common_header_version = "v000_000_000"
    expected_message = f"An issue occured whilst reading common header version '{invalid_common_header_version}'."

    schema_utils = init_schema_utils(
        common_header_version = invalid_common_header_version
    )

    with pytest.raises(SchemaError) as exc_info:
        schema_utils.get_common_header_df()
    assert (expected_message.format(directory = "") in str(exc_info.value))


def test_get_type_maps_df(init_schema_utils, type_maps_version, test_get_type_maps_df_input):
    version = type_maps_version
    expected_dataframe = test_get_type_maps_df_input[version]

    schema_utils = init_schema_utils(
        type_maps_version = version,
        use_json_feature = True
    )

    type_maps_df = schema_utils.get_type_maps_df()

    pd.testing.assert_frame_equal(expected_dataframe, type_maps_df)
    assert(expected_dataframe.equals(type_maps_df))


def test_get_type_maps_df_no_json_feature(init_schema_utils, type_maps_version, test_get_type_maps_df_input):
    version = type_maps_version
    expected_dataframe = test_get_type_maps_df_input[version]
    expected_dataframe.loc[expected_dataframe["type"] == "json", expected_dataframe.columns != "type"] = expected_dataframe.loc[expected_dataframe["type"] == "string", expected_dataframe.columns != "type"].values

    schema_utils = init_schema_utils(
        type_maps_version = version
    )

    type_maps_df = schema_utils.get_type_maps_df()

    pd.testing.assert_frame_equal(expected_dataframe, type_maps_df)
    assert(expected_dataframe.equals(type_maps_df))


def test_get_type_maps_df_no_json_feature_no_string_entry(tmp_path, init_schema_utils):
    version = "v001_000_000"
    expected_message = f"An issue occured whilst reading type maps version '{version}'."
    type_maps_path = tmp_path / "type_maps"
    type_maps_data = textwrap.dedent("""
        type,clickhouse_type,clickhouse_type_index,comment
        json,JSON,json,
        test_type_1,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_2,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_3,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_4,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_5,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_6,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_7,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_8,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_9,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
        test_type_10,String CODEC(ZSTD(1)),String CODEC(ZSTD(1)),
    """)

    Path(type_maps_path / version).mkdir(parents = True, exist_ok = True)

    schema_utils = init_schema_utils(
        type_maps_path = type_maps_path,
        type_maps_version = version
    )

    with open(type_maps_path / version / schema_utils.TYPE_MAPS_FILE_NAME, "w") as file:
        reader = csv.reader(io.StringIO(type_maps_data))
        writer = csv.writer(file)
        writer.writerows(reader)

    with pytest.raises(SchemaBuilderError) as exc_info:
        schema_utils.get_type_maps_df()
    assert (expected_message in str(exc_info.value))


def test_get_invalid_type_maps_df(init_schema_utils):
    invalid_type_maps_version = "v000_000_000"
    expected_message = f"An issue occured whilst reading type maps version '{invalid_type_maps_version}'."

    schema_utils = init_schema_utils(
        type_maps_version = invalid_type_maps_version
    )

    with pytest.raises(SchemaError) as exc_info:
        schema_utils.get_type_maps_df()
    assert (expected_message in str(exc_info.value))
