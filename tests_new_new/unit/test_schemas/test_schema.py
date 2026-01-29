from dfe_engine.schemas.schema import Schema


def test_schema_file_exists(init_schemas_dependencies, init_derived_schema, init_meta_schema):
    schema = Schema(
        name = "test_schema_file_exists",
        derived_schema_directory = init_derived_schema["derived_schema_directory"],
        derived_schema_version = init_derived_schema["derived_schema_version"],
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schema_name = init_meta_schema["meta_schema_name"],
        meta_schema_version = init_meta_schema["meta_schema_version"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"]
    )

    assert(schema._file_exists(init_derived_schema["derived_schema_path"]))
    assert(schema._file_exists(init_meta_schema["meta_schema_path"]))


def test_schema_file_not_exists(init_schemas_dependencies, init_derived_schema, init_meta_schema):
    schema = Schema(
        name = "test_schema_file_exists",
        derived_schema_directory = init_derived_schema["derived_schema_directory"],
        derived_schema_version = init_derived_schema["derived_schema_version"],
        derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
        meta_schema_name = init_meta_schema["meta_schema_name"],
        meta_schema_version = init_meta_schema["meta_schema_version"],
        meta_schemas_path = init_schemas_dependencies["meta_schemas_path"]
    )

    assert(schema._file_exists(init_derived_schema["derived_schema_path"]))
    assert(schema._file_exists(init_meta_schema["meta_schema_path"]))

# def test_schema_build_directory_not_exists(init_schemas_dependencies):
#     schema_builder = SchemaBuilder(
#         derived_schemas_path = init_schemas_dependencies["derived_schemas_path"],
#         meta_schemas_path = init_schemas_dependencies["meta_schemas_path"],
#         output_path = init_schemas_dependencies["output_path"],
#         schema_config_path = init_schemas_dependencies["schema_config_path"]
#     )

#     derived_schemas_path = init_schemas_dependencies["derived_schemas_path"] / "not_exists"
#     output_path = init_schemas_dependencies["output_path"] / "not_exists"

#     with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
#         schema_builder._directory_exists(derived_schemas_path)
#     assert(str(exc_info.value) == f"The directory '{derived_schemas_path}' could not be found.")

#     with pytest.raises(SchemaBuilderDirNotFoundError) as exc_info:
#         schema_builder._directory_exists(output_path)
#     assert(str(exc_info.value) == f"The directory '{output_path}' could not be found.")