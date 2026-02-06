import pytest

from dfe_engine.schemas.custom_exceptions import SchemaBuilderDirNotFoundError, SchemaBuilderNoSchemasToBuildError
from dfe_engine.schemas.schema_builder import SchemaBuilder

from ..templates.derived_schema import LOGS_TEST_DERIVED_001_000_000
from ..templates.dfe_package import DFE_PKG_SINGLE_SCHEMA, DFE_PKG_MULTI_SCHEMA, DFE_PKG_DUPLICATE_SCHEMA, DFE_PKG_NO_SCHEMA
from ..templates.meta_schema import LOGS_TEST_META


@pytest.fixture
def init_schema_builder(init_schema_paths):
    
    def _init_(
        derived_schema_filter = None,
        schema_filter = None
    ):
        schema_builder = SchemaBuilder(
            derived_schemas_path = init_schema_paths["derived_schemas_path"],
            meta_schemas_path = init_schema_paths["meta_schemas_path"],
            output_path = init_schema_paths["output_path"],
            schema_config_path = init_schema_paths["schema_config_path"],
            derived_schema_filter = derived_schema_filter,
            schema_filter = schema_filter
        )
        return schema_builder
    
    return _init_


def test_schema_build_directory_exists(init_schema_config, init_schema_builder):
    schema_config = init_schema_config(DFE_PKG_SINGLE_SCHEMA)
    schema_builder = init_schema_builder()

    assert(schema_builder._directory_exists(schema_config["derived_schemas_path"]))
    assert(schema_builder._directory_exists(schema_config["output_path"]))


def test_schema_build_directory_not_exists(init_schema_config, init_schema_builder):
    schema_config = init_schema_config(DFE_PKG_SINGLE_SCHEMA)
    schema_builder = init_schema_builder()

    derived_schemas_path = schema_config["derived_schemas_path"] / "not_exists"
    output_path = schema_config["output_path"] / "not_exists"

    with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
        schema_builder._directory_exists(derived_schemas_path)
    assert(str(exc_info.value) == f"The directory '{derived_schemas_path}' could not be found.")

    with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
        schema_builder._directory_exists(output_path)
    assert(str(exc_info.value) == f"The directory '{output_path}' could not be found.")


def test_schema_build_filter_schemas(init_schema_config, init_schema_builder, test_schema_build_filter_schemas_input):
    schema_filter = test_schema_build_filter_schemas_input["schema_filter"]
    expected_result = test_schema_build_filter_schemas_input["expected_result"]

    init_schema_config(DFE_PKG_MULTI_SCHEMA)
    schema_builder = init_schema_builder(schema_filter = schema_filter)

    filtered_schemas_names = [filtered_schema["name"] for filtered_schema in schema_builder._filter_schemas()]

    assert(expected_result == filtered_schemas_names)


def test_schema_build_filter_derived_schemas(init_schema_config, init_schema_builder, test_schema_build_filter_derived_schemas_input):
    derived_schema_filter = test_schema_build_filter_derived_schemas_input["derived_schema_filter"]
    expected_result = test_schema_build_filter_derived_schemas_input["expected_result"]
    
    init_schema_config(DFE_PKG_MULTI_SCHEMA)
    schema_builder = init_schema_builder(derived_schema_filter = derived_schema_filter)

    filtered_schemas_names = [filtered_schema["name"] for filtered_schema in schema_builder._filter_schemas()]

    assert(expected_result == filtered_schemas_names)


def test_schema_build(init_schema_config, init_derived_schema, init_meta_schema, init_schema_builder):
    init_schema_config(DFE_PKG_SINGLE_SCHEMA)
    init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    init_meta_schema(LOGS_TEST_META)
    schema_builder = init_schema_builder()

    schema_builder.build()

    # TODO: ADD BUILD VERIFICATIONS HERE


def test_schema_build_with_duplicate(init_schema_config, init_derived_schema, init_meta_schema, init_schema_builder):
    init_schema_config(DFE_PKG_DUPLICATE_SCHEMA)
    init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    init_meta_schema(LOGS_TEST_META)
    schema_builder = init_schema_builder()

    schema_builder.build()

    # TODO: ADD BUILD VERIFICATIONS HERE


def test_schema_build_no_schemas(init_schema_config, init_derived_schema, init_meta_schema, init_schema_builder):
    init_schema_config(DFE_PKG_NO_SCHEMA)
    init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    init_meta_schema(LOGS_TEST_META)
    schema_builder = init_schema_builder()

    with pytest.raises(SchemaBuilderNoSchemasToBuildError) as exc_info:
        schema_builder.build()
    
    assert(str(exc_info.value) == "No schemas found.")