import pandas as pd
import pytest

from dfe_engine.schemas.custom_exceptions import SchemaError, SchemaBuilderError
from dfe_engine.schemas.schema_field_definitions.common_header_fields import COMMON_HEADER_FIELDS
from dfe_engine.schemas.schema_field_definitions.derived_schema_fields import DERIVED_SCHEMA_FIELDS
from dfe_engine.schemas.schema_field_definitions.meta_schema_fields import META_SCHEMA_FIELDS
from dfe_engine.schemas.schema_field_definitions.type_maps_fields import TYPE_MAPS_FIELDS
from dfe_engine.schemas.schema_utils import SchemaUtils
from pathlib import Path

from ..templates.derived_schema import LOGS_TEST_DERIVED_001_000_000
from ..templates.meta_schema import LOGS_TEST_META_001_000_000


@pytest.fixture
def init_schema_utils():
    
    def _init_(
        common_header_path = None,
        common_header_version = None,
        type_maps_path = None,
        type_maps_version = None,
        use_json_feature = False
    ):
        schema_utils = SchemaUtils(
            common_header_path = common_header_path,
            common_header_version = common_header_version,
            type_maps_path = type_maps_path,
            type_maps_version = type_maps_version,
            use_json_feature = use_json_feature
        )
        return schema_utils
    
    return _init_


def test_get_common_header_df(init_schema_utils, common_header_version, test_get_common_header_df_input):
    version = common_header_version
    field_types = {field["name"]: field.get("type", "string") if (field.get("type", "string") == "string") else field.get("type", "").capitalize() for field in COMMON_HEADER_FIELDS}
    
    expected_dataframe = test_get_common_header_df_input[version].replace("", pd.NA).astype(field_types)

    schema_utils = init_schema_utils(
        common_header_version = version
    )

    common_header_df = schema_utils.get_common_header_df()

    pd.testing.assert_frame_equal(expected_dataframe, common_header_df)
    assert(expected_dataframe.equals(common_header_df))


def test_get_invalid_common_header_version_df(init_schema_utils):
    invalid_common_header_version = "v000_000_000"
    expected_message = f"An issue occured whilst reading common header version '{invalid_common_header_version}'."

    schema_utils = init_schema_utils(
        common_header_version = invalid_common_header_version
    )

    with pytest.raises(SchemaError) as exc_info:
        schema_utils.get_common_header_df()
    assert (expected_message.format() in str(exc_info.value))


def test_get_invalid_common_header_df(tmp_path, init_schema_utils, init_common_header, test_get_invalid_common_header_df_input):
    common_header_path = tmp_path / "common_header"
    expected_messages = test_get_invalid_common_header_df_input["raises"]["messages"]

    common_header = init_common_header(
        common_header_dict = test_get_invalid_common_header_df_input | {"common_header_path": common_header_path}
    )

    schema_utils = init_schema_utils(
        common_header_path = common_header_path,
        common_header_version = test_get_invalid_common_header_df_input["common_header_version"]
    )

    with pytest.raises(test_get_invalid_common_header_df_input["raises"]["exception"]) as exc_info:
        schema_utils.get_common_header_df()
    
    for expected_message in expected_messages:
        assert (expected_message.format(common_header_file_path = common_header["common_header_path"]) in str(exc_info.value.errors))


def test_get_derived_schema_df(init_schema_utils, derived_schema_name_version, test_get_derived_schemas_df_input):
    meta_schema_name, name, version, path = derived_schema_name_version
    field_types = {field["name"]: field.get("type", "string") if (field.get("type", "string") == "string") else field.get("type", "").capitalize() for field in DERIVED_SCHEMA_FIELDS}

    expected_dataframe = test_get_derived_schemas_df_input.get(meta_schema_name, {}).get(name, {}).get(version, {})

    schema_utils = init_schema_utils()

    derived_schema_df = schema_utils.get_derived_schema_df(
        derived_schema_path = Path(path) / meta_schema_name / name / version / f"{name}.csv"
    )

    if not(isinstance(expected_dataframe, pd.DataFrame)):
        pytest.skip(f"Derived schema '{name} - {version}' does not have a matching expected dataframe. Performed syntax check only.")
    
    expected_dataframe = expected_dataframe.replace("", pd.NA).astype(field_types)

    pd.testing.assert_frame_equal(expected_dataframe, derived_schema_df)
    assert(expected_dataframe.equals(derived_schema_df))


def test_get_invalid_derived_schema_path_df(init_schema_utils, tmp_path):
    invalid_derived_schema_name = "non-existant"
    invalid_meta_schema_name = "non-existant"
    invalid_derived_schema_path = tmp_path / invalid_meta_schema_name / invalid_derived_schema_name / "v001_000_000" / f"{invalid_derived_schema_name}.csv"
    expected_message = f"An issue occured whilst reading the derived schema at '{invalid_derived_schema_path}'."

    schema_utils = init_schema_utils()

    with pytest.raises(SchemaError) as exc_info:
        schema_utils.get_derived_schema_df(
            derived_schema_path = invalid_derived_schema_path
        )
    assert (expected_message in str(exc_info.value))


def test_get_invalid_derived_schema_df(tmp_path, init_schema_utils, init_derived_schema, test_get_invalid_derived_schema_df_input):
    derived_schema_path = tmp_path / "derived_schemas"
    expected_messages = test_get_invalid_derived_schema_df_input["raises"]["messages"]

    derived_schema = init_derived_schema(
        derived_schema_dict = test_get_invalid_derived_schema_df_input | {"derived_schema_path": derived_schema_path}
    )

    schema_utils = init_schema_utils()

    with pytest.raises(test_get_invalid_derived_schema_df_input["raises"]["exception"]) as exc_info:
        schema_utils.get_derived_schema_df(
            derived_schema_path = derived_schema["derived_schema_path"]
        )
    
    for expected_message in expected_messages:
        assert (expected_message.format(derived_schema_file_path = derived_schema["derived_schema_path"]) in str(exc_info.value.errors))


def test_get_meta_schema_df(init_schema_utils, meta_schema_name_version, test_get_meta_schemas_df_input):
    name, version, path = meta_schema_name_version
    field_types = {field["name"]: field.get("type", "string") if (field.get("type", "string") == "string") else field.get("type", "").capitalize() for field in META_SCHEMA_FIELDS}

    expected_dataframe = test_get_meta_schemas_df_input.get(name, {}).get(version, {})

    schema_utils = init_schema_utils()

    meta_schema_df = schema_utils.get_meta_schema_df(
        meta_schema_path = Path(path) / name / version / f"{name}.csv"
    )

    if not(isinstance(expected_dataframe, pd.DataFrame)):
        pytest.skip(f"Meta schema '{name} - {version}' does not have a matching expected dataframe. Performed syntax check only.")
    
    expected_dataframe = expected_dataframe.replace("", pd.NA).astype(field_types)

    pd.testing.assert_frame_equal(expected_dataframe, meta_schema_df)
    assert(expected_dataframe.equals(meta_schema_df))


def test_get_invalid_meta_schema_path_df(init_schema_utils, tmp_path):
    invalid_meta_schema_name = "non-existant"
    invalid_meta_schema_path = tmp_path / invalid_meta_schema_name / "v001_000_000" / f"{invalid_meta_schema_name}.csv"
    expected_message = f"An issue occured whilst reading the meta schema at '{invalid_meta_schema_path}'."

    schema_utils = init_schema_utils()

    with pytest.raises(SchemaError) as exc_info:
        schema_utils.get_meta_schema_df(
            meta_schema_path = invalid_meta_schema_path
        )
    assert (expected_message in str(exc_info.value))


def test_get_invalid_meta_schema_df(tmp_path, init_schema_utils, init_meta_schema, test_get_invalid_meta_schema_df_input):
    meta_schema_path = tmp_path / "meta_schemas"
    expected_messages = test_get_invalid_meta_schema_df_input["raises"]["messages"]

    meta_schema = init_meta_schema(
        meta_schema_dict = test_get_invalid_meta_schema_df_input | {"meta_schema_path": meta_schema_path}
    )

    schema_utils = init_schema_utils()

    with pytest.raises(test_get_invalid_meta_schema_df_input["raises"]["exception"]) as exc_info:
        schema_utils.get_meta_schema_df(
            meta_schema_path = meta_schema["meta_schema_path"]
        )
    
    for expected_message in expected_messages:
        assert (expected_message.format(meta_schema_file_path = meta_schema["meta_schema_path"]) in str(exc_info.value.errors))


def test_get_type_maps_df(init_schema_utils, type_maps_version, test_get_type_maps_df_input):
    version = type_maps_version
    field_types = {field["name"]: field.get("type", "string") if (field.get("type", "string") == "string") else field.get("type", "").capitalize() for field in TYPE_MAPS_FIELDS}
    
    expected_dataframe = test_get_type_maps_df_input[version].replace("", pd.NA).astype(field_types)

    schema_utils = init_schema_utils(
        type_maps_version = version,
        use_json_feature = True
    )

    type_maps_df = schema_utils.get_type_maps_df()

    pd.testing.assert_frame_equal(expected_dataframe, type_maps_df)
    assert(expected_dataframe.equals(type_maps_df))


def test_get_type_maps_df_no_json_feature(init_schema_utils, type_maps_version, test_get_type_maps_df_input):
    version = type_maps_version
    field_types = {field["name"]: field.get("type", "string") if (field.get("type", "string") == "string") else field.get("type", "").capitalize() for field in TYPE_MAPS_FIELDS}
    
    expected_dataframe = test_get_type_maps_df_input[version].replace("", pd.NA).astype(field_types)
    expected_dataframe.loc[expected_dataframe["type"] == "json", expected_dataframe.columns != "type"] = expected_dataframe.loc[expected_dataframe["type"] == "string", expected_dataframe.columns != "type"].values

    schema_utils = init_schema_utils(
        type_maps_version = version
    )

    type_maps_df = schema_utils.get_type_maps_df()

    pd.testing.assert_frame_equal(expected_dataframe, type_maps_df)
    assert(expected_dataframe.equals(type_maps_df))


def test_get_type_maps_df_no_json_feature_no_string_entry(tmp_path, init_schema_utils, init_type_maps, test_get_type_maps_no_string_df_input):
    type_maps_path = tmp_path / "type_maps"
    expected_message = f"An issue occured whilst reading type maps version '{test_get_type_maps_no_string_df_input["type_maps_version"]}'."

    init_type_maps(
        type_maps_dict = test_get_type_maps_no_string_df_input | {"type_maps_path": type_maps_path}
    )

    schema_utils = init_schema_utils(
        type_maps_path = type_maps_path,
        type_maps_version = test_get_type_maps_no_string_df_input["type_maps_version"],
    )

    with pytest.raises(SchemaBuilderError) as exc_info:
        schema_utils.get_type_maps_df()
    assert (expected_message in str(exc_info.value))


def test_get_invalid_type_maps_version_df(init_schema_utils):
    invalid_type_maps_version = "v000_000_000"
    expected_message = f"An issue occured whilst reading type maps version '{invalid_type_maps_version}'."

    schema_utils = init_schema_utils(
        type_maps_version = invalid_type_maps_version
    )

    with pytest.raises(SchemaError) as exc_info:
        schema_utils.get_type_maps_df()
    assert (expected_message in str(exc_info.value))


def test_get_invalid_type_maps_df(tmp_path, init_schema_utils, init_type_maps, test_get_invalid_type_maps_df_input):
    type_maps_path = tmp_path / "type_maps"
    expected_messages = test_get_invalid_type_maps_df_input["raises"]["messages"]

    type_maps = init_type_maps(
        type_maps_dict = test_get_invalid_type_maps_df_input | {"type_maps_path": type_maps_path}
    )

    schema_utils = init_schema_utils(
        type_maps_path = type_maps_path,
        type_maps_version = test_get_invalid_type_maps_df_input["type_maps_version"],
        use_json_feature = True
    )

    with pytest.raises(test_get_invalid_type_maps_df_input["raises"]["exception"]) as exc_info:
        schema_utils.get_type_maps_df()

    for expected_message in expected_messages:
        assert (expected_message.format(type_maps_file_path = type_maps["type_maps_path"]) in str(exc_info.value.errors))


def test_create_combined_df(init_derived_schema, init_meta_schema, init_schema_utils):
    derived_schema = init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    meta_schema = init_meta_schema(LOGS_TEST_META_001_000_000)
    schema_utils = init_schema_utils()

    common_header_df = schema_utils.get_common_header_df()
    type_maps_df = schema_utils.get_type_maps_df()

    schema_utils.create_combined_df(
        common_header_df = common_header_df,
        derived_schema_path = derived_schema["derived_schema_path"],
        meta_schema_path = meta_schema["meta_schema_path"]
    )