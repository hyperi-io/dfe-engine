import logging
import pytest
import pandas as pd
from dfe_engine.schema.schema_util import SchemaUtils, SchemaValidationError
from typing import Dict, Any, Set

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)


@pytest.fixture
def es_schema_dict():
    return {
        "template": {
            "settings": {"index": {"query": {"default_field": "some_default_field"}}},
            "mappings": {
                "properties": {"common_field": {}, "@timestamp": {}, "timestamp": {}}
            },
        },
        "_meta": "meta_value",
        "analyzer": "analyzer_value",
        "scaling_factor": "scaling_value",
        "search_analyzer": "search_analyzer_value",
    }


@pytest.fixture
def common_header_schema_df():
    data = {"column": ["common_field"]}
    return pd.DataFrame(data)


def test_manage_nested_dict_action_find(es_schema_dict):
    assert (
        SchemaUtils.manage_nested_dict_action(
            es_schema_dict, "template.settings.index.query.default_field", "find"
        )
        is True
    )


def test_manage_nested_dict_action_get(es_schema_dict):
    assert (
        SchemaUtils.manage_nested_dict_action(
            es_schema_dict, "template.settings.index.query.default_field", "get"
        )
        == "some_default_field"
    )


def test_manage_nested_dict_action_delete(es_schema_dict):
    SchemaUtils.manage_nested_dict_action(
        es_schema_dict, "template.settings.index.query.default_field", "delete"
    )
    assert (
        str(
            SchemaUtils.manage_nested_dict_action(
                es_schema_dict, "template.settings.index.query.default_field", "get"
            )
        )
        == "None"
    )


def test_manage_nested_dict_action_set(es_schema_dict):
    SchemaUtils.manage_nested_dict_action(
        es_schema_dict,
        "template.settings.index.query.default_field",
        "set",
        "new_default_field",
    )
    assert (
        SchemaUtils.manage_nested_dict_action(
            es_schema_dict, "template.settings.index.query.default_field", "get"
        )
        == "new_default_field"
    )


def test_dict_remove_key_recursively_meta(es_schema_dict):
    SchemaUtils.dict_remove_key_recursively(es_schema_dict, "_meta")
    assert "_meta" not in es_schema_dict


def test_dict_remove_key_recursively_analyzer(es_schema_dict):
    SchemaUtils.dict_remove_key_recursively(es_schema_dict, "analyzer")
    assert "analyzer" not in es_schema_dict


def test_dict_remove_key_recursively_scaling_factor(es_schema_dict):
    SchemaUtils.dict_remove_key_recursively(es_schema_dict, "scaling_factor")
    assert "scaling_factor" not in es_schema_dict


def test_dict_remove_key_recursively_search_analyzer(es_schema_dict):
    SchemaUtils.dict_remove_key_recursively(es_schema_dict, "search_analyzer")
    assert "search_analyzer" not in es_schema_dict


def test_remove_overlap_fields_with_common_header(
    es_schema_dict, common_header_schema_df
):
    for index, row in common_header_schema_df.iterrows():
        header_col = row["column"]
        if header_col in es_schema_dict["template"]["mappings"]["properties"]:
            del es_schema_dict["template"]["mappings"]["properties"][header_col]
    if "@timestamp" in es_schema_dict["template"]["mappings"]["properties"]:
        del es_schema_dict["template"]["mappings"]["properties"]["@timestamp"]
    if "timestamp" in es_schema_dict["template"]["mappings"]["properties"]:
        del es_schema_dict["template"]["mappings"]["properties"]["timestamp"]

    assert "common_field" not in es_schema_dict["template"]["mappings"]["properties"]
    assert "@timestamp" not in es_schema_dict["template"]["mappings"]["properties"]
    assert "timestamp" not in es_schema_dict["template"]["mappings"]["properties"]


def test_dict_merge_no_conflict():
    dict1 = {"a": 1, "b": {"c": 3}}
    dict2 = {"b": {"d": 4}, "e": 5}
    expected_result = {"a": 1, "b": {"c": 3, "d": 4}, "e": 5}
    result = SchemaUtils.dict_merge(dict1, dict2)
    assert result == expected_result


def test_dict_merge_with_conflict_overwrite():
    dict1 = {"a": 1, "b": {"c": 3}}
    dict2 = {"b": {"c": 4}, "e": 5}
    expected_result = {"a": 1, "b": {"c": 4}, "e": 5}
    result = SchemaUtils.dict_merge(dict1, dict2, overwrite=True)
    assert result == expected_result


def test_dict_merge_with_conflict_no_overwrite():
    dict1 = {"a": 1, "b": {"c": 3}}
    dict2 = {"b": {"c": 4}, "e": 5}
    with pytest.raises(SchemaValidationError, match=r"Conflict at b.c"):
        SchemaUtils.dict_merge(dict1, dict2, overwrite=False)


def test_dict_merge_empty_dict2():
    dict1 = {"a": 1, "b": {"c": 3}}
    dict2 = {}
    expected_result = {"a": 1, "b": {"c": 3}}
    result = SchemaUtils.dict_merge(dict1, dict2)
    assert result == expected_result


def test_dict_merge_empty_dict1():
    dict1 = {}
    dict2 = {"b": {"c": 4}, "e": 5}
    expected_result = {"b": {"c": 4}, "e": 5}
    result = SchemaUtils.dict_merge(dict1, dict2)
    assert result == expected_result


def test_dict_merge_nested_conflict():
    dict1 = {"a": {"b": {"c": 1}}}
    dict2 = {"a": {"b": {"c": 2}}}
    with pytest.raises(SchemaValidationError, match=r"Conflict at a.b.c"):
        SchemaUtils.dict_merge(dict1, dict2, overwrite=False)


def test_dict_merge_nested_no_conflict():
    dict1 = {"a": {"b": {"c": 1}}}
    dict2 = {"a": {"b": {"d": 2}}}
    expected_result = {"a": {"b": {"c": 1, "d": 2}}}
    result = SchemaUtils.dict_merge(dict1, dict2)
    assert result == expected_result


def test_sql_replacement():
    raw_column = "o365.audit.Severity"
    expected_sql_column = "o365_audit_severity"
    actual_sql_column = SchemaUtils.sql_column_fix_name(raw_column)
    assert actual_sql_column == expected_sql_column


def test_remove_overlap_fields_with_common_header():
    add_fields_df = pd.DataFrame(
        {
            "column": ["o365.audit.Severity", "timestamp"],
            "type": ["string_fast_lowcardinality", "datetime"],
            "index_order": ["", ""],
            "os_order": ["", ""],
            "comment": ["Severity", "timestamp duplicate"],
        }
    )
    schema_name = "test_schema"
    common_header_schema_df = pd.DataFrame({"column": ["timestamp"]})
    cleaned_df = SchemaUtils.strip_duplicate_config_columns_from_non_common_df(
        add_fields_df, common_header_schema_df, schema_name, logger, is_ch_flag=True
    )
    print(cleaned_df)
    assert "timestamp" in add_fields_df["column"].values, (
        "Test failed: 'timestamp' should still be present in the common header schema."
    )

    expected_columns = ["timestamp", "o365.audit.Severity"]
    assert cleaned_df["column"].tolist() == expected_columns, (
        "Test failed: The overlapping 'timestamp' column was not removed correctly from additional fields."
    )

    removed_comment = add_fields_df.loc[
        add_fields_df["column"] == "timestamp", "comment"
    ].values[0]
    assert removed_comment == "timestamp duplicate", (
        "Test failed: The removed 'timestamp' column comment was not correct."
    )


@pytest.mark.parametrize(
    "input_properties, expected_output",
    [
        (
            {"field1": {"type": "keyword"}, "field2": {"type": "integer"}},
            {"field1": {"type": "keyword"}, "field2": {"type": "integer"}},
        ),
        (
            {
                "container": {
                    "properties": {
                        "cpu": {"properties": {"usage": {"type": "float"}}},
                        "name": {"type": "keyword"},
                    }
                }
            },
            {
                "container.cpu.usage": {"type": "float"},
                "container.name": {"type": "keyword"},
            },
        ),
        (
            {
                "level1": {
                    "properties": {
                        "level2": {
                            "properties": {
                                "level3": {"properties": {"field": {"type": "keyword"}}}
                            }
                        }
                    }
                }
            },
            {"level1.level2.level3.field": {"type": "keyword"}},
        ),
        ({}, {}),
        (
            {"field": {"type": "text"}, "field2": {"type": "keyword"}},
            {"field": {"type": "text"}, "field2": {"type": "keyword"}},
        ),
    ],
)
def test_flatten_properties(
    input_properties: Dict[str, Any], expected_output: Dict[str, Any]
):
    """
    Test the flatten_properties function with various inputs to ensure it correctly flattens nested dictionaries.

    Args:
        input_properties (Dict[str, Any]): The input nested dictionary.
        expected_output (Dict[str, Any]): The expected output flattened dictionary.
    """
    result = SchemaUtils.flatten_properties(input_properties)
    assert result == expected_output, f"Expected {expected_output}, but got {result}"


def test_flatten_properties_with_different_separator():
    """
    Test the flatten_properties function with a different separator to verify custom separator handling.
    """
    input_properties = {
        "container": {
            "properties": {
                "cpu": {"properties": {"usage": {"type": "float"}}},
                "name": {"type": "keyword"},
            }
        }
    }
    expected_output = {
        "container|cpu|usage": {"type": "float"},
        "container|name": {"type": "keyword"},
    }
    result = SchemaUtils.flatten_properties(input_properties, sep="|")
    assert result == expected_output, f"Expected {expected_output}, but got {result}"


def test_flatten_properties_with_custom_property_key():
    """
    Test the flatten_properties function using a custom property key to ensure flexibility.
    """
    input_properties = {"level1": {"custom_key": {"field": {"type": "keyword"}}}}
    expected_output = {"level1.field": {"type": "keyword"}}
    result = SchemaUtils.flatten_properties(input_properties, property_key="custom_key")
    assert result == expected_output, f"Expected {expected_output}, but got {result}"


@pytest.mark.parametrize(
    "input_properties, expected_output",
    [
        (
            {"field1": {"type": "keyword"}, "field2": {"type": "integer"}},
            {"field1", "field2"},
        ),
        (
            {
                "container": {
                    "properties": {
                        "cpu": {"properties": {"usage": {"type": "float"}}},
                        "name": {"type": "keyword"},
                    }
                }
            },
            {"container.cpu.usage", "container.name"},
        ),
        (
            {
                "level1": {
                    "properties": {
                        "level2": {
                            "properties": {
                                "level3": {"properties": {"field": {"type": "keyword"}}}
                            }
                        }
                    }
                }
            },
            {"level1.level2.level3.field"},
        ),
        ({}, set()),
        (
            {"field": {"type": "text"}, "field2": {"type": "keyword"}},
            {"field", "field2"},
        ),
    ],
)
def test_extract_field_paths(
    input_properties: Dict[str, Any], expected_output: Set[str]
):
    """
    Test the extract_field_paths function with various inputs to ensure it correctly extracts all nested field paths.

    Args:
        input_properties (Dict[str, Any]): The input nested dictionary.
        expected_output (Set[str]): The expected set of extracted field paths.
    """
    result = SchemaUtils.extract_field_paths(input_properties)
    assert result == expected_output, f"Expected {expected_output}, but got {result}"


def test_extract_field_paths_with_different_separator():
    """
    Test the extract_field_paths function with a different separator to verify custom separator handling.
    """
    input_properties = {
        "container": {
            "properties": {
                "cpu": {"properties": {"usage": {"type": "float"}}},
                "name": {"type": "keyword"},
            }
        }
    }
    expected_output = {"container|cpu|usage", "container|name"}
    result = SchemaUtils.extract_field_paths(input_properties, sep="|")
    assert result == expected_output, f"Expected {expected_output}, but got {result}"



from dfe_engine.schema.schema_update import SchemaModifier
import os
import tempfile


@pytest.fixture(scope="session")
def test_database_name_unit():
    """Generate a unique test database name for unit tests."""
    return f"test_schema_modifier_unit_{os.getpid()}"


def test_schema_utils_basic_functionality():
    """Test basic SchemaUtils functionality that doesn't require ClickHouse."""
    
    input_properties = {
        "user": {
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "keyword"}
            }
        }
    }
    expected_output = {"user.id", "user.name"}
    result = SchemaUtils.extract_field_paths(input_properties)
    assert result == expected_output, f"Expected {expected_output}, but got {result}"
