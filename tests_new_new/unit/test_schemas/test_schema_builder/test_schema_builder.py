import pytest

from dfe_engine.schemas.custom_exceptions import SchemaBuilderNoSchemasToBuildError
from dfe_engine.schemas.schema_builder import SchemaBuilder

from ..templates.derived_schema import LOGS_TEST_DERIVED_001_000_000
from ..templates.dfe_package import DFE_PKG_SINGLE_SCHEMA, DFE_PKG_DUPLICATE_SCHEMA, DFE_PKG_NO_SCHEMA
from ..templates.meta_schema import LOGS_TEST_META_001_000_000


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


def test_schema_build(init_schema_config, init_derived_schema, init_meta_schema, init_schema_builder):
    init_schema_config(DFE_PKG_SINGLE_SCHEMA)
    init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    init_meta_schema(LOGS_TEST_META_001_000_000)
    schema_builder = init_schema_builder()

    schema_builder.build()

    assert(False)

    # TODO: ADD BUILD VERIFICATIONS HERE


def test_schema_build_with_duplicate(init_schema_config, init_derived_schema, init_meta_schema, init_schema_builder):
    init_schema_config(DFE_PKG_DUPLICATE_SCHEMA)
    init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    init_meta_schema(LOGS_TEST_META_001_000_000)
    schema_builder = init_schema_builder()

    schema_builder.build()

    # TODO: ADD BUILD VERIFICATIONS HERE


def test_schema_build_no_schemas(init_schema_config, init_derived_schema, init_meta_schema, init_schema_builder):
    init_schema_config(DFE_PKG_NO_SCHEMA)
    init_derived_schema(LOGS_TEST_DERIVED_001_000_000)
    init_meta_schema(LOGS_TEST_META_001_000_000)
    schema_builder = init_schema_builder()

    with pytest.raises(SchemaBuilderNoSchemasToBuildError) as exc_info:
        schema_builder.build()
    
    assert(str(exc_info.value) == "No schemas found.")