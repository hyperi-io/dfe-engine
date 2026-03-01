"""Tests for the Schema Loader v2 (schema_loader.py)."""

import pytest

from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError
from dfe_engine.source.models import SchemaColumn
from dfe_engine.yaml_utils import yaml_dump


@pytest.fixture
def tmp_schema(tmp_path):
    """Helper to create a temporary schema YAML file."""

    def _write(columns: list[dict], filename: str = "schema.yaml"):
        path = tmp_path / filename
        yaml_dump({"columns": columns}, path)
        return path

    return _write


# ── load_columns ────────────────────────────────────────────────────


class TestLoadColumns:
    def test_load_basic(self, tmp_schema):
        path = tmp_schema([
            {"name": "user_name", "type": "string", "use_case": "dimension"},
            {"name": "message", "type": "text", "use_case": "fulltext"},
        ])
        columns = SchemaLoader.load_columns(path)
        assert len(columns) == 2
        assert all(isinstance(c, SchemaColumn) for c in columns)
        assert columns[0].name == "user_name"
        assert columns[0].type == "string"
        assert columns[0].use_case == "dimension"
        assert columns[1].name == "message"

    def test_load_with_attributes(self, tmp_schema):
        path = tmp_schema([
            {"name": "x", "type": "string", "attribute": ["lowcardinality", "not_null"]},
        ])
        columns = SchemaLoader.load_columns(path)
        assert columns[0].attribute == ["lowcardinality", "not_null"]

    def test_load_with_all_fields(self, tmp_schema):
        path = tmp_schema([{
            "name": "ts",
            "type": "timestamp",
            "default": "now64(3)",
            "order": 0,
            "comment": "@generated: now64(3)",
        }])
        col = SchemaLoader.load_columns(path)[0]
        assert col.default == "now64(3)"
        assert col.order == 0
        assert col.comment == "@generated: now64(3)"

    def test_load_with_ch_override(self, tmp_schema):
        path = tmp_schema([
            {"name": "status", "type": "integer", "ch_override": "UInt16"},
        ])
        col = SchemaLoader.load_columns(path)[0]
        assert col.ch_override == "UInt16"

    def test_file_not_found(self):
        with pytest.raises(SchemaLoadError, match="not found"):
            SchemaLoader.load_columns("/nonexistent/path.yaml")

    def test_missing_columns_key(self, tmp_path):
        path = tmp_path / "bad.yaml"
        yaml_dump({"other": "data"}, path)
        with pytest.raises(SchemaLoadError, match="columns"):
            SchemaLoader.load_columns(path)

    def test_invalid_column_data(self, tmp_schema):
        path = tmp_schema([
            "not a dict",
        ])
        with pytest.raises(SchemaLoadError, match="must be a dict"):
            SchemaLoader.load_columns(path)

    def test_invalid_column_schema(self, tmp_schema):
        path = tmp_schema([
            {"name": "x"},  # missing required 'type' field
        ])
        with pytest.raises(SchemaLoadError, match="Invalid column"):
            SchemaLoader.load_columns(path)


# ── load_profile ────────────────────────────────────────────────────


class TestLoadProfile:
    def test_load_timeseries(self):
        columns = SchemaLoader.load_profile("timeseries")
        names = [c.name for c in columns]
        assert "_timestamp_load" in names
        assert "_timestamp" in names
        assert "_org_id" in names
        assert "_source" in names
        assert "_raw" in names
        assert "_json" in names
        assert "_tags" in names

    def test_load_minimal(self):
        columns = SchemaLoader.load_profile("minimal")
        names = [c.name for c in columns]
        assert "_timestamp_load" in names
        assert "_timestamp" in names
        assert "_org_id" in names
        assert "_uuid" in names
        # minimal should NOT have _raw, _json, _tags, _source
        assert "_raw" not in names
        assert "_source" not in names

    def test_load_passthrough(self):
        columns = SchemaLoader.load_profile("passthrough")
        names = [c.name for c in columns]
        assert "_timestamp_load" in names
        assert "_uuid" in names
        assert "_org_id" in names
        assert "_json" in names
        # passthrough should NOT have _timestamp, _raw, _source
        assert "_timestamp" not in names
        assert "_raw" not in names
        assert "_source" not in names

    def test_profile_not_found(self):
        with pytest.raises(SchemaLoadError, match="not found"):
            SchemaLoader.load_profile("nonexistent_profile")

    def test_custom_profiles_dir(self, tmp_path):
        yaml_dump(
            {"columns": [{"name": "x", "type": "string"}]},
            tmp_path / "custom.yaml",
        )
        columns = SchemaLoader.load_profile("custom", profiles_dir=tmp_path)
        assert len(columns) == 1
        assert columns[0].name == "x"

    def test_timeseries_order_fields(self):
        columns = SchemaLoader.load_profile("timeseries")
        order_cols = SchemaLoader.get_order_by_columns(columns)
        assert order_cols[0] == "_timestamp_load"
        assert order_cols[1] == "_timestamp"


# ── apply_derived_schema ────────────────────────────────────────────


class TestApplyDerived:
    def test_override_existing_column(self, tmp_schema):
        base = [
            SchemaColumn(name="x", type="string"),
            SchemaColumn(name="y", type="integer"),
        ]
        derived_path = tmp_schema([
            {"name": "x", "type": "string", "attribute": ["lowcardinality"]},
        ], filename="derived.yaml")

        result = SchemaLoader.apply_derived_schema(base, derived_path)
        assert len(result) == 2
        x_col = next(c for c in result if c.name == "x")
        assert x_col.attribute == ["lowcardinality"]

    def test_preserves_order(self, tmp_schema):
        base = [
            SchemaColumn(name="a", type="string"),
            SchemaColumn(name="b", type="string"),
            SchemaColumn(name="c", type="string"),
        ]
        derived_path = tmp_schema([
            {"name": "c", "type": "string", "use_case": "dimension"},
        ], filename="derived.yaml")

        result = SchemaLoader.apply_derived_schema(base, derived_path)
        assert [c.name for c in result] == ["a", "b", "c"]

    def test_missing_derived_returns_base(self, tmp_path):
        base = [SchemaColumn(name="x", type="string")]
        result = SchemaLoader.apply_derived_schema(base, tmp_path / "missing.yaml")
        assert result == base


# ── apply_additional_fields ─────────────────────────────────────────


class TestApplyAdditional:
    def test_append_new_columns(self, tmp_schema):
        base = [SchemaColumn(name="x", type="string")]
        additional_path = tmp_schema([
            {"name": "y", "type": "integer"},
            {"name": "z", "type": "boolean"},
        ], filename="additional.yaml")

        result = SchemaLoader.apply_additional_fields(base, additional_path)
        assert len(result) == 3
        assert [c.name for c in result] == ["x", "y", "z"]

    def test_override_existing_on_conflict(self, tmp_schema):
        base = [SchemaColumn(name="x", type="string")]
        additional_path = tmp_schema([
            {"name": "x", "type": "text", "use_case": "fulltext"},
        ], filename="additional.yaml")

        result = SchemaLoader.apply_additional_fields(base, additional_path)
        assert len(result) == 1
        assert result[0].type == "text"
        assert result[0].use_case == "fulltext"

    def test_missing_additional_returns_base(self, tmp_path):
        base = [SchemaColumn(name="x", type="string")]
        result = SchemaLoader.apply_additional_fields(base, tmp_path / "missing.yaml")
        assert result == base


# ── compose ─────────────────────────────────────────────────────────


class TestCompose:
    def test_profile_columns_first(self):
        profile = [
            SchemaColumn(name="_ts", type="timestamp"),
            SchemaColumn(name="_org", type="string"),
        ]
        source = [
            SchemaColumn(name="user_name", type="string"),
            SchemaColumn(name="message", type="text"),
        ]
        result = SchemaLoader.compose(profile, source)
        assert [c.name for c in result] == ["_ts", "_org", "user_name", "message"]

    def test_duplicate_dropped(self):
        profile = [SchemaColumn(name="_org", type="string")]
        source = [
            SchemaColumn(name="_org", type="text"),  # duplicate — should be dropped
            SchemaColumn(name="data", type="string"),
        ]
        result = SchemaLoader.compose(profile, source)
        assert len(result) == 2
        assert result[0].name == "_org"
        assert result[0].type == "string"  # profile value wins
        assert result[1].name == "data"


# ── validate_columns ────────────────────────────────────────────────


class TestValidateColumns:
    def test_valid_columns(self):
        from dfe_engine.source.type_registry import TypeRegistry

        registry = TypeRegistry.default()
        columns = [
            SchemaColumn(name="x", type="string", use_case="dimension"),
            SchemaColumn(name="y", type="integer"),
        ]
        errors = SchemaLoader.validate_columns(columns, registry)
        assert errors == []

    def test_duplicate_names(self):
        from dfe_engine.source.type_registry import TypeRegistry

        registry = TypeRegistry.default()
        columns = [
            SchemaColumn(name="x", type="string"),
            SchemaColumn(name="x", type="integer"),
        ]
        errors = SchemaLoader.validate_columns(columns, registry)
        assert any("Duplicate" in e for e in errors)

    def test_invalid_use_case(self):
        from dfe_engine.source.type_registry import TypeRegistry

        registry = TypeRegistry.default()
        columns = [
            SchemaColumn(name="x", type="integer", use_case="fulltext"),
        ]
        errors = SchemaLoader.validate_columns(columns, registry)
        assert len(errors) > 0


# ── get_order_by_columns ────────────────────────────────────────────


class TestGetOrderByColumns:
    def test_sorted_by_order(self):
        columns = [
            SchemaColumn(name="c", type="string", order=2),
            SchemaColumn(name="a", type="string", order=0),
            SchemaColumn(name="b", type="string", order=1),
            SchemaColumn(name="d", type="string"),  # no order
        ]
        result = SchemaLoader.get_order_by_columns(columns)
        assert result == ["a", "b", "c"]

    def test_empty_when_no_order_fields(self):
        columns = [
            SchemaColumn(name="x", type="string"),
            SchemaColumn(name="y", type="integer"),
        ]
        result = SchemaLoader.get_order_by_columns(columns)
        assert result == []
