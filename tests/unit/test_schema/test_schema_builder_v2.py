"""Tests for SchemaBuilderV2 (schema_builder_v2.py)."""

import pytest

from dfe_engine.schema.schema_builder_v2 import (
    SchemaBuildError,
    SchemaBuildResult,
    SchemaBuilderV2,
)
from dfe_engine.source.models import SchemaColumn, Source
from dfe_engine.source.type_registry import TypeRegistry
from dfe_engine.yaml_utils import yaml_dump


@pytest.fixture
def registry() -> TypeRegistry:
    return TypeRegistry.default()


@pytest.fixture
def builder(registry: TypeRegistry) -> SchemaBuilderV2:
    return SchemaBuilderV2(registry=registry)


@pytest.fixture
def schemas_dir(tmp_path):
    """Create a temp schemas directory with meta_schema + derived."""
    # meta_schema
    yaml_dump(
        {
            "columns": [
                {"name": "user_name", "type": "string", "use_case": "dimension"},
                {"name": "message", "type": "text", "use_case": "fulltext"},
                {"name": "event_id", "type": "integer"},
            ]
        },
        tmp_path / "meta.yaml",
    )

    # derived_schema (override message to text_search)
    yaml_dump(
        {
            "columns": [
                {"name": "message", "type": "text", "use_case": "text_search"},
            ]
        },
        tmp_path / "derived.yaml",
    )

    # additional_fields
    yaml_dump(
        {
            "columns": [
                {"name": "severity", "type": "string", "attribute": ["lowcardinality"], "use_case": "dimension"},
            ]
        },
        tmp_path / "additional.yaml",
    )

    return tmp_path


def _make_source(
    schemas_dir=None,
    *,
    meta_schema=None,
    derived_schema=None,
    additional_fields=None,
    header_type="timeseries",
    ttl_days=90,
    engine="MergeTree",
    sigma_mappings=None,
) -> Source:
    """Helper to create a Source with schema config."""
    schema_config = {}
    if meta_schema:
        schema_config["meta_schema"] = str(meta_schema)
    if derived_schema:
        schema_config["derived_schema"] = str(derived_schema)
    if additional_fields:
        schema_config["additional_fields"] = str(additional_fields)
    schema_config["ttl_days"] = ttl_days
    schema_config["engine"] = engine

    data = {
        "source": "test_source",
        "header": {"type": header_type, "version": "1.0.0"},
        "schema": schema_config,
    }
    if sigma_mappings:
        data["sigma"] = {"custom_mappings": sigma_mappings}

    return Source.model_validate(data)


# ── Build ───────────────────────────────────────────────────────────


class TestBuild:
    def test_basic_build_with_meta_schema(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml")
        result = builder.build(source)

        assert isinstance(result, SchemaBuildResult)
        assert result.source_name == "test_source"
        assert result.validation_errors == []

        # Profile columns + source columns
        names = [c.name for c in result.columns]
        assert "_timestamp_load" in names  # from timeseries profile
        assert "_timestamp" in names
        assert "user_name" in names  # from meta_schema
        assert "message" in names

        # DDL generated
        assert "CREATE TABLE IF NOT EXISTS" in result.create_table_ddl
        assert "test_source" in result.create_table_ddl

    def test_build_with_derived_and_additional(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(
            meta_schema="meta.yaml",
            derived_schema="derived.yaml",
            additional_fields="additional.yaml",
        )
        result = builder.build(source)

        names = [c.name for c in result.columns]
        # derived overrides message use_case to text_search
        msg_col = next(c for c in result.columns if c.name == "message")
        assert msg_col.use_case == "text_search"

        # additional adds severity
        assert "severity" in names

        # DDL should have text_search index
        assert "ngrams(3)" in result.create_table_ddl

    def test_build_minimal_profile(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml", header_type="minimal")
        result = builder.build(source)

        names = [c.name for c in result.columns]
        assert "_timestamp_load" in names
        assert "_uuid" in names
        assert "_raw" not in names  # minimal has no _raw

    def test_build_passthrough_profile(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml", header_type="passthrough")
        result = builder.build(source)

        names = [c.name for c in result.columns]
        assert "_json" in names
        assert "_timestamp" not in names  # passthrough has no _timestamp

    def test_build_no_meta_schema(self, registry):
        """Source with no meta_schema → only profile columns."""
        builder = SchemaBuilderV2(registry=registry)
        source = _make_source()
        result = builder.build(source)

        names = [c.name for c in result.columns]
        # Only timeseries profile columns
        assert "_timestamp_load" in names
        assert "_org_id" in names
        assert "user_name" not in names

    def test_build_engine_config(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(
            meta_schema="meta.yaml",
            engine="SharedMergeTree",
        )
        result = builder.build(source)
        assert "SharedMergeTree" in result.create_table_ddl

    def test_build_ttl_config(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml", ttl_days=365)
        result = builder.build(source)
        assert "INTERVAL 365 DAY" in result.create_table_ddl

    def test_build_profile_metadata_in_comment(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml")
        result = builder.build(source)
        assert "@profile: timeseries" in result.create_table_ddl
        assert "@profile_version: 1.0.0" in result.create_table_ddl


# ── Sigma View ──────────────────────────────────────────────────────


class TestSigmaView:
    def test_sigma_view_generated(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(
            meta_schema="meta.yaml",
            sigma_mappings={"User": "user_name", "EventID": "event_id"},
        )
        result = builder.build(source)

        assert result.sigma_view_ddl is not None
        assert "test_source_sigma" in result.sigma_view_ddl
        assert "`user_name` AS `User`" in result.sigma_view_ddl
        assert "`event_id` AS `EventID`" in result.sigma_view_ddl

    def test_no_sigma_config(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml")
        result = builder.build(source)
        assert result.sigma_view_ddl is None


# ── Validation ──────────────────────────────────────────────────────


class TestValidation:
    def test_validation_errors_reported(self, registry, tmp_path):
        """Invalid use_case → validation error in result."""
        yaml_dump(
            {
                "columns": [
                    {"name": "x", "type": "integer", "use_case": "fulltext"},
                ]
            },
            tmp_path / "bad_meta.yaml",
        )
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=tmp_path)
        source = _make_source(meta_schema="bad_meta.yaml")
        result = builder.build(source)
        assert len(result.validation_errors) > 0


# ── Error Handling ──────────────────────────────────────────────────


class TestErrors:
    def test_invalid_profile(self, registry):
        builder = SchemaBuilderV2(registry=registry)
        data = {
            "source": "bad_source",
            "header": {"type": "nonexistent_profile"},
            "schema": {},
        }
        source = Source.model_validate(data)
        with pytest.raises(SchemaBuildError, match="profile"):
            builder.build(source)

    def test_missing_meta_schema_file(self, registry, tmp_path):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=tmp_path)
        source = _make_source(meta_schema="missing.yaml")
        with pytest.raises(SchemaBuildError, match="meta_schema"):
            builder.build(source)


# ── ALTER TABLE ─────────────────────────────────────────────────────


class TestAlterTable:
    def test_generate_alter_add(self, registry):
        builder = SchemaBuilderV2(registry=registry)
        source = _make_source()
        col = SchemaColumn(name="new_field", type="string", use_case="dimension")
        ddl = builder.generate_alter_add(source, col)
        assert "ADD COLUMN" in ddl
        assert "`new_field`" in ddl

    def test_generate_alter_modify(self, registry):
        builder = SchemaBuilderV2(registry=registry)
        source = _make_source()
        col = SchemaColumn(name="user_name", type="string", attribute=["lowcardinality"])
        ddl = builder.generate_alter_modify(source, col)
        assert "MODIFY COLUMN" in ddl
        assert "LowCardinality" in ddl


# ── build_ddl_only ──────────────────────────────────────────────────


class TestBuildDDLOnly:
    def test_ddl_from_precomposed_columns(self, registry):
        builder = SchemaBuilderV2(registry=registry)
        columns = [
            SchemaColumn(name="_ts", type="timestamp", order=0),
            SchemaColumn(name="x", type="string"),
        ]
        ddl = builder.build_ddl_only(columns, "my_table")
        assert "CREATE TABLE IF NOT EXISTS {db}.my_table" in ddl
        assert "`_ts`" in ddl
        assert "`x`" in ddl
