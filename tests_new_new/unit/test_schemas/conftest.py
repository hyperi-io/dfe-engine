import csv
import io
import pytest
import yaml

from pathlib import Path
from templates.derived_schema import DERIVED_SCHEMA
from templates.dfe_package import DFE_PACKAGE
from templates.meta_schema import META_SCHEMA


@pytest.fixture
def init_schemas_dependencies(tmp_path):
    derived_schemas_path = Path(tmp_path / "derived_schemas")
    derived_schemas_path.mkdir(exist_ok = True)

    meta_schemas_path = Path(tmp_path / "meta_schemas")
    meta_schemas_path.mkdir(exist_ok = True)

    output_path = Path(tmp_path / "output")
    output_path.mkdir(exist_ok = True)

    schema_config_dir = Path(tmp_path / "config")
    schema_config_path = Path(schema_config_dir / "dfe_package.yaml")
    schema_config_dir.mkdir(exist_ok = True)
    with open(schema_config_path, "w") as file:
        yaml.dump(DFE_PACKAGE, file, sort_keys = False)
    
    yield {
        "derived_schemas_path": derived_schemas_path,
        "meta_schemas_path": meta_schemas_path,
        "output_path": output_path,
        "schema_config_path": schema_config_path
    }


@pytest.fixture
def init_derived_schema(init_schemas_dependencies):
    derived_schema_directory = "logs_test/logs_test_derived"
    derived_schema_version = "v001.000.000"

    derived_schema_path = init_schemas_dependencies["derived_schemas_path"] / derived_schema_directory / derived_schema_version.replace(".", "_") / f"{derived_schema_directory.split("/")[-1]}.csv"

    derived_schema_path.parent.mkdir(parents = True, exist_ok = True)
    with open(derived_schema_path, "w") as file:
        reader = csv.reader(io.StringIO(DERIVED_SCHEMA))
        writer = csv.writer(file)
        writer.writerows(reader)
    
    yield {
        "derived_schema_directory": derived_schema_directory,
        "derived_schema_path": derived_schema_path,
        "derived_schema_version": derived_schema_version
    }


@pytest.fixture
def init_meta_schema(init_schemas_dependencies):
    meta_schema_name = "logs_test"
    meta_schema_version = "v001.000.000"

    meta_schema_path = init_schemas_dependencies["meta_schemas_path"] / meta_schema_name / meta_schema_version.replace(".", "_") / f"{meta_schema_name}.csv"

    meta_schema_path.parent.mkdir(parents = True, exist_ok = True)
    with open(meta_schema_path, "w") as file:
        reader = csv.reader(io.StringIO(META_SCHEMA))
        writer = csv.writer(file)
        writer.writerows(reader)
    
    yield {
        "meta_schema_name": meta_schema_name,
        "meta_schema_path": meta_schema_path,
        "meta_schema_version": meta_schema_version
    }


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