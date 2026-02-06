import pytest


TEST_SCHEMA_BUILD_FILTER_SCHEMAS_INPUT = [
    {
        "name": "test_no_filter",
        "schema_filter": None,
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_single_filter",
        "schema_filter": "logs_test_1",
        "expected_result": [
            "logs_test_1"
        ]
    },
    {
        "name": "test_multiple_filter",
        "schema_filter": "logs_test_1,logs_test_2,logs_exclude_test_1",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_wildcard_filter",
        "schema_filter": "logs_test_*",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10"
        ]
    },
    {
        "name": "test_multiple_filter_with_wildcard",
        "schema_filter": "logs_test_*,logs_exclude_test_1",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_no_matching_filter",
        "schema_filter": "logs_test_no_match",
        "expected_result": []
    }
]

@pytest.fixture(params = TEST_SCHEMA_BUILD_FILTER_SCHEMAS_INPUT, ids = lambda x: x["name"])
def test_schema_build_filter_schemas_input(request):
    return request.param


TEST_SCHEMA_BUILD_FILTER_DERIVED_SCHEMAS_INPUT = [
    {
        "name": "test_no_derived_filter",
        "derived_schema_filter": None,
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_single_derived_filter",
        "derived_schema_filter": "logs_test_derived",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10"
        ]
    },
    {
        "name": "test_multiple_derived_filter",
        "derived_schema_filter": "logs_test_derived,logs_exclude_test_derived",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_wildcard_derived_filter",
        "derived_schema_filter": "logs_*",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_multiple_filter_with_wildcard",
        "derived_schema_filter": "logs_*,logs_exclude_test_derived",
        "expected_result": [
            "logs_test_1",
            "logs_test_2",
            "logs_test_3",
            "logs_test_4",
            "logs_test_5",
            "logs_test_6",
            "logs_test_7",
            "logs_test_8",
            "logs_test_9",
            "logs_test_10",
            "logs_exclude_test_1"
        ]
    },
    {
        "name": "test_no_matching_filter",
        "derived_schema_filter": "logs_test_no_match",
        "expected_result": []
    }
]

@pytest.fixture(params = TEST_SCHEMA_BUILD_FILTER_DERIVED_SCHEMAS_INPUT, ids = lambda x: x["name"])
def test_schema_build_filter_derived_schemas_input(request):
    return request.param