"""Tests for SchemaBuilderV2 (schema_builder_v2.py)."""

import pytest

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import FieldMapRegistry
from dfe_engine.schema.schema_builder_v2 import (
    SchemaBuildError,
    SchemaBuilderV2,
    SchemaBuildResult,
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
                {
                    "name": "severity",
                    "type": "string",
                    "attribute": ["lowcardinality"],
                    "use_case": "dimension",
                },
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
    mapping_standards=None,
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
        "match": {"field": "tags.collector.type", "value": "test_source"},
        "header": {"type": header_type, "version": "1.0.0"},
        "schema": schema_config,
    }
    if sigma_mappings:
        data["sigma"] = {"custom_mappings": sigma_mappings}
    if mapping_standards:
        data["mapping_standards"] = mapping_standards

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

    def test_build_meta_schema_registry_path_without_suffix(self, registry, tmp_path):
        meta_dir = tmp_path / "meta" / "aws"
        meta_dir.mkdir(parents=True)
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [{"name": "event_name", "type": "string"}],
                    }
                },
            },
            meta_dir / "cloudtrail.yaml",
        )
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=tmp_path)
        source = _make_source(
            meta_schema="meta/aws/cloudtrail",
            header_type="minimal",
        )
        result = builder.build(source)
        assert "event_name" in [c.name for c in result.columns]

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
            "match": {"field": "tags.collector.type", "value": "bad_source"},
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


# ── View DDL Integration (FieldMapRegistry) ────────────────────────


@pytest.fixture
def field_maps_dir(tmp_path):
    d = tmp_path / "field-maps"
    d.mkdir()
    return d


@pytest.fixture
def fm_registry(field_maps_dir):
    FieldMapRegistry.reset_instance()
    reg = FieldMapRegistry(
        field_maps_directory=field_maps_dir,
        writable=True,
        refresh_interval=0,
    )
    yield reg
    reg.close()
    FieldMapRegistry.reset_instance()


class TestViewDDLIntegration:
    def test_no_views_without_registry(self, registry, schemas_dir):
        """Without field_map_registry, view_ddls is empty."""
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(
            meta_schema="meta.yaml",
            mapping_standards=["sigma"],
        )
        result = builder.build(source)
        assert result.view_ddls == {}

    def test_no_views_without_mapping_standards(self, registry, schemas_dir, fm_registry):
        """With registry but no mapping_standards on source, no views."""
        builder = SchemaBuilderV2(
            registry=registry,
            schemas_base_dir=schemas_dir,
            field_map_registry=fm_registry,
        )
        source = _make_source(meta_schema="meta.yaml")
        result = builder.build(source)
        assert result.view_ddls == {}

    def test_generates_views_for_declared_standards(self, registry, schemas_dir, fm_registry):
        """With registry + mapping_standards, views are generated."""
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                mappings={"EventID": "event_id", "User": "user_name"},
            )
        )
        fm_registry.save_map(
            FieldMap(
                standard="ecs",
                mappings={"source.ip": "source_ip"},
            )
        )
        builder = SchemaBuilderV2(
            registry=registry,
            schemas_base_dir=schemas_dir,
            field_map_registry=fm_registry,
        )
        source = _make_source(
            meta_schema="meta.yaml",
            mapping_standards=["sigma", "ecs"],
        )
        result = builder.build(source)

        assert "sigma" in result.view_ddls
        assert "ecs" in result.view_ddls
        assert "test_source_sigma" in result.view_ddls["sigma"]
        assert "test_source_ecs" in result.view_ddls["ecs"]
        assert "`user_name` AS `User`" in result.view_ddls["sigma"]

    def test_skips_standard_with_no_maps(self, registry, schemas_dir, fm_registry):
        """If a declared standard has no maps, it's excluded from view_ddls."""
        fm_registry.save_map(FieldMap(standard="sigma", mappings={"X": "x"}))
        builder = SchemaBuilderV2(
            registry=registry,
            schemas_base_dir=schemas_dir,
            field_map_registry=fm_registry,
        )
        source = _make_source(
            meta_schema="meta.yaml",
            mapping_standards=["sigma", "ecs"],
        )
        result = builder.build(source)

        assert "sigma" in result.view_ddls
        assert "ecs" not in result.view_ddls

    def test_legacy_sigma_view_coexists_with_view_ddls(self, registry, schemas_dir, fm_registry):
        """Legacy sigma_view_ddl and new view_ddls are both populated."""
        fm_registry.save_map(FieldMap(standard="sigma", mappings={"EventID": "event_id"}))
        builder = SchemaBuilderV2(
            registry=registry,
            schemas_base_dir=schemas_dir,
            field_map_registry=fm_registry,
        )
        source = _make_source(
            meta_schema="meta.yaml",
            sigma_mappings={"User": "user_name"},
            mapping_standards=["sigma"],
        )
        result = builder.build(source)

        # Legacy path
        assert result.sigma_view_ddl is not None
        assert "`user_name` AS `User`" in result.sigma_view_ddl
        # New path
        assert "sigma" in result.view_ddls
        assert "`event_id` AS `EventID`" in result.view_ddls["sigma"]

    def test_source_specific_overrides_in_views(self, registry, schemas_dir, fm_registry):
        """Source-specific field map overrides default in view DDL."""
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                mappings={"EventID": "event_id", "User": "user_name"},
            )
        )
        fm_registry.save_map(
            FieldMap(
                standard="sigma",
                source="test_source",
                mappings={"EventID": "cs_event_id"},
            )
        )
        builder = SchemaBuilderV2(
            registry=registry,
            schemas_base_dir=schemas_dir,
            field_map_registry=fm_registry,
        )
        source = _make_source(
            meta_schema="meta.yaml",
            mapping_standards=["sigma"],
        )
        result = builder.build(source)

        assert "`cs_event_id` AS `EventID`" in result.view_ddls["sigma"]
        assert "`user_name` AS `User`" in result.view_ddls["sigma"]


class TestLoadColumnsForSourceVersion:
    def test_loads_schema_from_requested_version_snapshot(self, registry, schemas_dir):
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [
                            {"name": "user_name", "type": "string", "use_case": "dimension"},
                        ]
                    }
                },
            },
            schemas_dir / "meta_v1.yaml",
        )
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = Source.model_validate(
            {
                "source": "versioned_src",
                "match": {"field": "tags.collector.type", "value": "versioned_src"},
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {
                            "meta_schema": "meta_v1.yaml",
                            "meta_schema_version": "1.0.0",
                        },
                    },
                    "2.0.0": {
                        "date_time": "2026-02-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {"additional_fields": "additional.yaml"},
                    },
                },
            }
        )
        v1_names = {
            c.name for c in builder.load_columns_for_source_version(source, source_version="1.0.0")
        }
        v2_names = {
            c.name for c in builder.load_columns_for_source_version(source, source_version="2.0.0")
        }
        assert "user_name" in v1_names
        assert "severity" in v2_names
        assert "user_name" not in v2_names

    def test_unknown_version_raises(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = _make_source(meta_schema="meta.yaml")
        with pytest.raises(SchemaBuildError, match=r"Source version '9\.9\.9'"):
            builder.load_columns_for_source_version(source, source_version="9.9.9")

    def test_build_for_source_version_uses_snapshot(self, registry, schemas_dir):
        builder = SchemaBuilderV2(registry=registry, schemas_base_dir=schemas_dir)
        source = Source.model_validate(
            {
                "source": "versioned_src",
                "match": {"field": "tags.collector.type", "value": "versioned_src"},
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": "2026-01-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {"meta_schema": "meta.yaml"},
                    },
                    "2.0.0": {
                        "date_time": "2026-02-01",
                        "header": {"type": "minimal", "version": "1.0.0"},
                        "schema": {"additional_fields": "additional.yaml"},
                    },
                },
            }
        )
        r1 = builder.build_for_source_version(source, source_version="1.0.0")
        r2 = builder.build_for_source_version(source, source_version="2.0.0")
        assert "user_name" in [c.name for c in r1.columns]
        assert "severity" in [c.name for c in r2.columns]
        assert r1.create_table_ddl
        assert r2.create_table_ddl
