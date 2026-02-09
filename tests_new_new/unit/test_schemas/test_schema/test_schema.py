import pytest

from dfe_engine.schemas.schema import Schema
from pathlib import Path


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