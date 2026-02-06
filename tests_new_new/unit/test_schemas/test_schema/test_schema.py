import pytest

from dfe_engine.schemas.custom_exceptions import SchemaFileNotFoundError
from dfe_engine.schemas.schema import Schema
from pathlib import Path

from ..templates.derived_schema import LOGS_TEST_DERIVED_001_000_000
from ..templates.dfe_package import DFE_PKG_SINGLE_SCHEMA
from ..templates.meta_schema import LOGS_TEST_META


@pytest.fixture
def init_schema(init_schema_config, init_derived_schema, init_meta_schema):

    def _init_(schema_name, schema_config_data, derived_schema_data, meta_schema_data):
        schema_config = init_schema_config(schema_config_data)
        derived_schema = init_derived_schema(derived_schema_data)
        meta_schema = init_meta_schema(meta_schema_data)

        schema = Schema(
            name = schema_name,
            derived_schema_directory = Path(derived_schema["meta_schema_name"]) / derived_schema["derived_schema_name"],
            derived_schema_version = derived_schema["derived_schema_version"],
            derived_schemas_path = schema_config["derived_schemas_path"],
            meta_schema_name = meta_schema["meta_schema_name"],
            meta_schema_version = meta_schema["meta_schema_version"],
            meta_schemas_path = schema_config["meta_schemas_path"]
        )

        return schema

    return _init_


def test_schema_file_exists(init_schema):
    schema = init_schema(
        schema_name = "test_schema_file_exists",
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_000,
        meta_schema_data = LOGS_TEST_META
    )

    assert(schema._file_exists(schema.info["derived_schema"]["path"]))
    assert(schema._file_exists(schema.info["meta_schema"]["path"]))


def test_schema_file_not_exists(init_schema):
    schema = init_schema(
        schema_name = "test_schema_file_not_exists",
        schema_config_data = DFE_PKG_SINGLE_SCHEMA,
        derived_schema_data = LOGS_TEST_DERIVED_001_000_000,
        meta_schema_data = LOGS_TEST_META
    )

    with pytest.raises(SchemaFileNotFoundError) as exc_info:
        schema._file_exists(schema.info["derived_schema"]["path"] / "non-existant")
    
    assert(str(exc_info.value) == f"The file '{schema.info["derived_schema"]["path"] / "non-existant"}' could not be found.")

    with pytest.raises(SchemaFileNotFoundError) as exc_info:
        schema._file_exists(schema.info["meta_schema"]["path"]/ "non-existant")
    
    assert(str(exc_info.value) == f"The file '{schema.info["meta_schema"]["path"] / "non-existant"}' could not be found.")