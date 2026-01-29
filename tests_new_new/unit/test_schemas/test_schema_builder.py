import pytest

from dfe_engine.schemas.custom_exceptions import SchemaBuilderDirNotFoundError
from dfe_engine.schemas.schema_builder import SchemaBuilder


def test_schema_build_directory_exists(init_schemas_dependencies):
    schema_builder = SchemaBuilder(
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"],
        output_path = init_schemas_dependencies["output_path"],
        schema_config_path = init_schemas_dependencies["schema_config_path"]
    )

    assert(schema_builder._directory_exists(init_schemas_dependencies["derived_schemas_path"]))
    assert(schema_builder._directory_exists(init_schemas_dependencies["output_path"]))


def test_schema_build_directory_not_exists(init_schemas_dependencies):
    schema_builder = SchemaBuilder(
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"],
        output_path = init_schemas_dependencies["output_path"],
        schema_config_path = init_schemas_dependencies["schema_config_path"]
    )

    derived_schemas_path = init_schemas_dependencies["derived_schemas_path"] / "not_exists"
    output_path = init_schemas_dependencies["output_path"] / "not_exists"

    with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
        schema_builder._directory_exists(derived_schemas_path)
    assert(str(exc_info.value) == f"The directory '{derived_schemas_path}' could not be found.")

    with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
        schema_builder._directory_exists(output_path)
    assert(str(exc_info.value) == f"The directory '{output_path}' could not be found.")


def test_schema_build_filter_schemas(init_schemas_dependencies, test_schema_build_filter_schemas_input):
    schema_filter = test_schema_build_filter_schemas_input["schema_filter"]
    expected_result = test_schema_build_filter_schemas_input["expected_result"]

    schema_builder = SchemaBuilder(
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"],
        output_path = init_schemas_dependencies["output_path"],
        schema_config_path = init_schemas_dependencies["schema_config_path"],
        schema_filter = schema_filter
    )

    filtered_schemas_names = [filtered_schema["name"] for filtered_schema in schema_builder._filter_schemas()]

    assert(expected_result == filtered_schemas_names)


def test_schema_build_filter_derived_schemas(init_schemas_dependencies, test_schema_build_filter_derived_schemas_input):
    derived_schema_filter = test_schema_build_filter_derived_schemas_input["derived_schema_filter"]
    expected_result = test_schema_build_filter_derived_schemas_input["expected_result"]

    schema_builder = SchemaBuilder(
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"],
        output_path = init_schemas_dependencies["output_path"],
        schema_config_path = init_schemas_dependencies["schema_config_path"],
        derived_schema_filter = derived_schema_filter
    )

    filtered_schemas_names = [filtered_schema["name"] for filtered_schema in schema_builder._filter_schemas()]

    assert(expected_result == filtered_schemas_names)


def test_schema_build(init_schemas_dependencies, init_derived_schema, init_meta_schema):
    schema_builder = SchemaBuilder(
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"],
        output_path = init_schemas_dependencies["output_path"],
        schema_config_path = init_schemas_dependencies["schema_config_path"]
    )

    schema_builder.build()
    assert(False)