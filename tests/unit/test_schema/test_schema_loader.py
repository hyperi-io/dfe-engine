"""Tests for the Schema Loader v2 (schema_loader.py)."""

import pytest

from dfe_engine.schema.schema_loader import (
    SchemaLoader,
    SchemaLoadError,
    _resolve_package_schemas_root,
    _resolve_profiles_dir,
    _resolve_schemas_root,
    is_shipped_schema,
)
from dfe_engine.source.models import SchemaColumn
from dfe_engine.yaml_utils import yaml_dump
from tests.unit.test_schema.conftest import requires_schemas


@pytest.fixture
def tmp_schema(tmp_path):
    """Helper to create a temporary schema YAML file."""

    def _write(columns: list[dict], filename: str = "schema.yaml"):
        path = tmp_path / filename
        yaml_dump({"columns": columns}, path)
        return path

    return _write


@pytest.fixture
def tmp_versioned_schema(tmp_path):
    """Helper to create a versioned schema YAML file."""

    def _write(data: dict, filename: str = "versioned.yaml"):
        path = tmp_path / filename
        yaml_dump(data, path)
        return path

    return _write


# ── load_columns ────────────────────────────────────────────────────


class TestLoadColumns:
    def test_load_basic(self, tmp_schema):
        path = tmp_schema(
            [
                {"name": "user_name", "type": "string", "use_case": "dimension"},
                {"name": "message", "type": "text", "use_case": "fulltext"},
            ]
        )
        columns = SchemaLoader.load_columns(path)
        assert len(columns) == 2
        assert all(isinstance(c, SchemaColumn) for c in columns)
        assert columns[0].name == "user_name"
        assert columns[0].type == "string"
        assert columns[0].use_case == "dimension"
        assert columns[1].name == "message"

    def test_load_with_attributes(self, tmp_schema):
        path = tmp_schema(
            [
                {"name": "x", "type": "string", "attribute": ["lowcardinality", "not_null"]},
            ]
        )
        columns = SchemaLoader.load_columns(path)
        assert columns[0].attribute == ["lowcardinality", "not_null"]

    def test_load_with_all_fields(self, tmp_schema):
        path = tmp_schema(
            [
                {
                    "name": "ts",
                    "type": "timestamp",
                    "default": "now64(3)",
                    "order": 0,
                    "expr": "@generated: now64(3)",
                    "comment": "Insertion timestamp",
                }
            ]
        )
        col = SchemaLoader.load_columns(path)[0]
        assert col.default == "now64(3)"
        assert col.order == 0
        assert col.expr == "@generated: now64(3)"
        assert col.comment == "Insertion timestamp"

    def test_load_with_ch_override(self, tmp_schema):
        path = tmp_schema(
            [
                {"name": "status", "type": "integer", "ch_override": "UInt16"},
            ]
        )
        col = SchemaLoader.load_columns(path)[0]
        assert col.ch_override == "UInt16"

    def test_file_not_found(self):
        with pytest.raises(SchemaLoadError, match="not found"):
            SchemaLoader.load_columns("/nonexistent/path.yaml")

    def test_missing_columns_key(self, tmp_path):
        path = tmp_path / "bad.yaml"
        yaml_dump({"other": "data"}, path)
        with pytest.raises(SchemaLoadError, match=r"columns.*versions"):
            SchemaLoader.load_columns(path)

    def test_invalid_column_data(self, tmp_schema):
        path = tmp_schema(
            [
                "not a dict",
            ]
        )
        with pytest.raises(SchemaLoadError, match="must be a dict"):
            SchemaLoader.load_columns(path)

    def test_invalid_column_schema(self, tmp_schema):
        path = tmp_schema(
            [
                {"name": "x"},  # missing required 'type' field
            ]
        )
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

    def test_load_minimal_registry_path(self):
        columns = SchemaLoader.load_profile("common-header/minimal", profile_version="1.0.0")
        names = [c.name for c in columns]
        assert "_json" in names
        assert "_uuid" in names

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
        derived_path = tmp_schema(
            [
                {"name": "x", "type": "string", "attribute": ["lowcardinality"]},
            ],
            filename="derived.yaml",
        )

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
        derived_path = tmp_schema(
            [
                {"name": "c", "type": "string", "use_case": "dimension"},
            ],
            filename="derived.yaml",
        )

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
        additional_path = tmp_schema(
            [
                {"name": "y", "type": "integer"},
                {"name": "z", "type": "boolean"},
            ],
            filename="additional.yaml",
        )

        result = SchemaLoader.apply_additional_fields(base, additional_path)
        assert len(result) == 3
        assert [c.name for c in result] == ["x", "y", "z"]

    def test_override_existing_on_conflict(self, tmp_schema):
        base = [SchemaColumn(name="x", type="string")]
        additional_path = tmp_schema(
            [
                {"name": "x", "type": "text", "use_case": "fulltext"},
            ],
            filename="additional.yaml",
        )

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

    def test_exclude_drops_profile_columns(self):
        profile = [
            SchemaColumn(name="_ts", type="timestamp"),
            SchemaColumn(name="_raw", type="text"),
            SchemaColumn(name="_tags", type="json"),
        ]
        source = [SchemaColumn(name="rule_id", type="string")]
        result = SchemaLoader.compose(profile, source, exclude=["_raw", "_tags"])
        assert [c.name for c in result] == ["_ts", "rule_id"]

    def test_exclude_of_absent_column_is_a_no_op(self):
        # A schema must compose onto any profile; `minimal` has no _raw.
        profile = [SchemaColumn(name="_ts", type="timestamp")]
        source = [SchemaColumn(name="rule_id", type="string")]
        result = SchemaLoader.compose(profile, source, exclude=["_raw"])
        assert [c.name for c in result] == ["_ts", "rule_id"]

    def test_exclude_does_not_touch_source_columns(self):
        profile = [SchemaColumn(name="_ts", type="timestamp")]
        source = [SchemaColumn(name="severity", type="string")]
        result = SchemaLoader.compose(profile, source, exclude=["severity"])
        assert [c.name for c in result] == ["_ts", "severity"]


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


# ── Package resolution ────────────────────────────────────────────


@requires_schemas
class TestPackageResolution:
    """Test profile resolution chain: env var → dfe-schemas package → bundled."""

    def test_resolve_profiles_dir_finds_the_package(self):
        """With the package installed, resolution returns its common-header."""

        resolved = _resolve_profiles_dir()
        # Should end with common-header (either the package or bundled)
        assert resolved.is_dir()
        # Should contain timeseries.yaml
        assert (resolved / "timeseries.yaml").exists()

    def test_resolve_schemas_root(self):
        """Should find the dfe-schemas package root."""

        root = _resolve_schemas_root()
        # In the test environment, the dfe-schemas package is installed
        if root is not None:
            assert root.is_dir()
            assert (root / "common-header").is_dir()

    def test_env_var_override(self, tmp_path, monkeypatch):
        """DFE_SCHEMAS_DIR env var should override the package."""
        # Create a custom schemas dir with common-header
        header_dir = tmp_path / "common-header"
        header_dir.mkdir()
        yaml_dump(
            {"columns": [{"name": "_custom", "type": "string"}]},
            header_dir / "custom_profile.yaml",
        )
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path))

        columns = SchemaLoader.load_profile("custom_profile")
        assert len(columns) == 1
        assert columns[0].name == "_custom"

    def test_env_var_profiles_dir_resolution(self, tmp_path, monkeypatch):
        """DFE_SCHEMAS_DIR should be checked for common-header subdir."""
        header_dir = tmp_path / "common-header"
        header_dir.mkdir()
        yaml_dump(
            {"columns": [{"name": "_ts", "type": "timestamp"}]},
            header_dir / "timeseries.yaml",
        )
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path))

        resolved = _resolve_profiles_dir()
        assert resolved == header_dir

    def test_explicit_profiles_dir_wins(self, tmp_path):
        """Explicit profiles_dir argument should skip resolution chain."""
        yaml_dump(
            {"columns": [{"name": "explicit", "type": "string"}]},
            tmp_path / "test.yaml",
        )
        columns = SchemaLoader.load_profile("test", profiles_dir=tmp_path)
        assert columns[0].name == "explicit"

    def test_package_profiles_identical_to_bundled(self):
        """Packaged and bundled profiles should have the same columns."""
        from pathlib import Path

        bundled_dir = (
            Path(__file__).resolve().parents[3] / "src" / "dfe_engine" / "schema" / "profiles"
        )
        package_root = _resolve_package_schemas_root()
        if package_root is None:
            pytest.skip("dfe-schemas package not installed")
        package_dir = package_root / "common-header"

        for profile in ("timeseries", "minimal", "passthrough"):
            bundled = SchemaLoader.load_profile(profile, profiles_dir=bundled_dir)
            packaged = SchemaLoader.load_profile(profile, profiles_dir=package_dir)
            bundled_names = [c.name for c in bundled]
            packaged_names = [c.name for c in packaged]
            assert bundled_names == packaged_names, f"Mismatch in {profile} profile"


# ── is_shipped_schema ────────────────────────────────────────────


class TestIsShippedSchema:
    """Test read-only shipped schema detection."""

    def test_bundled_profile_is_shipped(self):
        """Bundled profile files should be identified as shipped."""
        from pathlib import Path

        bundled = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "dfe_engine"
            / "schema"
            / "profiles"
            / "timeseries.yaml"
        )
        assert is_shipped_schema(bundled) is True

    def test_package_file_is_shipped(self):
        """Files inside the dfe-schemas package should be identified as shipped."""
        package_root = _resolve_package_schemas_root()
        if package_root is None:
            pytest.skip("dfe-schemas package not installed")
        packaged = package_root / "common-header" / "timeseries.yaml"
        if not packaged.exists():
            pytest.skip("dfe-schemas package carries no common-header/timeseries.yaml")
        assert is_shipped_schema(packaged) is True

    def test_user_file_is_not_shipped(self, tmp_path):
        """User-created files should NOT be identified as shipped."""
        user_file = tmp_path / "my_profile.yaml"
        user_file.write_text("columns: []")
        assert is_shipped_schema(user_file) is False


# ── Version tree ───────────────────────────────────────────────────


class TestVersionTree:
    """Test version tree schema loading (versions.<ver>.columns)."""

    def test_load_current_default(self, tmp_versioned_schema):
        """When no version specified, uses file's current marker."""
        path = tmp_versioned_schema(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "summary": "Initial",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b", "type": "integer"},
                        ],
                    },
                },
            }
        )
        columns = SchemaLoader.load_columns(path)
        assert [c.name for c in columns] == ["a", "b"]

    def test_explicit_version_overrides_current(self, tmp_versioned_schema):
        """Explicit version arg selects a different version snapshot."""
        path = tmp_versioned_schema(
            {
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "summary": "Initial",
                        "columns": [
                            {"name": "a", "type": "string"},
                        ],
                    },
                    "2.0.0": {
                        "date": "2026-03-02",
                        "type": "addition",
                        "summary": "Added b",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b", "type": "integer"},
                        ],
                    },
                },
            }
        )
        # Default (current=2.0.0): a, b
        cols = SchemaLoader.load_columns(path)
        assert [c.name for c in cols] == ["a", "b"]

        # Pin to 1.0.0: just a
        cols = SchemaLoader.load_columns(path, version="1.0.0")
        assert [c.name for c in cols] == ["a"]

    def test_dropped_column_between_versions(self, tmp_versioned_schema):
        """A column present in v1 can be absent in v2."""
        path = tmp_versioned_schema(
            {
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "summary": "Initial with legacy",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "legacy", "type": "text"},
                        ],
                    },
                    "2.0.0": {
                        "date": "2026-03-02",
                        "type": "model",
                        "summary": "Removed legacy",
                        "columns": [
                            {"name": "a", "type": "string"},
                        ],
                    },
                },
            }
        )
        v1 = SchemaLoader.load_columns(path, version="1.0.0")
        assert [c.name for c in v1] == ["a", "legacy"]

        v2 = SchemaLoader.load_columns(path, version="2.0.0")
        assert [c.name for c in v2] == ["a"]

    def test_changed_column_type_between_versions(self, tmp_versioned_schema):
        """A column's type can change between versions."""
        path = tmp_versioned_schema(
            {
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "columns": [
                            {"name": "_raw", "type": "text", "use_case": "text_search"},
                        ],
                    },
                    "2.0.0": {
                        "date": "2026-03-02",
                        "type": "model",
                        "columns": [
                            {"name": "_raw", "type": "string"},
                        ],
                    },
                },
            }
        )
        v1 = SchemaLoader.load_columns(path, version="1.0.0")
        assert v1[0].type == "text"
        assert v1[0].use_case == "text_search"

        v2 = SchemaLoader.load_columns(path, version="2.0.0")
        assert v2[0].type == "string"
        assert v2[0].use_case is None

    def test_multi_version_history(self, tmp_versioned_schema):
        """Full lifecycle across 3 versions."""
        path = tmp_versioned_schema(
            {
                "current": "3.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b", "type": "integer"},
                        ],
                    },
                    "2.0.0": {
                        "date": "2026-02-15",
                        "type": "addition",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b", "type": "integer"},
                            {"name": "c", "type": "boolean"},
                        ],
                    },
                    "3.0.0": {
                        "date": "2026-03-15",
                        "type": "model",
                        "summary": "Replaced b with b_new, added c",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b_new", "type": "float"},
                            {"name": "c", "type": "boolean"},
                        ],
                    },
                },
            }
        )
        v1 = SchemaLoader.load_columns(path, version="1.0.0")
        assert [c.name for c in v1] == ["a", "b"]

        v2 = SchemaLoader.load_columns(path, version="2.0.0")
        assert [c.name for c in v2] == ["a", "b", "c"]

        v3 = SchemaLoader.load_columns(path, version="3.0.0")
        assert [c.name for c in v3] == ["a", "b_new", "c"]

    def test_version_not_found_raises(self, tmp_versioned_schema):
        """Requesting a nonexistent version raises SchemaLoadError."""
        path = tmp_versioned_schema(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "columns": [{"name": "a", "type": "string"}],
                    },
                },
            }
        )
        with pytest.raises(SchemaLoadError, match=r"Version '9\.9\.9' not found"):
            SchemaLoader.load_columns(path, version="9.9.9")

    def test_version_missing_columns_key_raises(self, tmp_versioned_schema):
        """Version entry without columns raises SchemaLoadError."""
        path = tmp_versioned_schema(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        # no columns key!
                    },
                },
            }
        )
        with pytest.raises(SchemaLoadError, match="must contain a 'columns' key"):
            SchemaLoader.load_columns(path)

    def test_unversioned_flat_file_still_works(self, tmp_schema):
        """Files without version tree return all columns (backward compat)."""
        path = tmp_schema(
            [
                {"name": "x", "type": "string"},
                {"name": "y", "type": "integer"},
            ]
        )
        columns = SchemaLoader.load_columns(path)
        assert len(columns) == 2

    def test_load_profile_with_version(self, tmp_path):
        """load_profile passes version through to load_columns."""
        yaml_dump(
            {
                "current": "1.1.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                    "1.1.0": {
                        "date": "2026-02-15",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b", "type": "string"},
                        ],
                    },
                },
            },
            tmp_path / "test_profile.yaml",
        )
        # Default (current=1.1.0): both columns
        cols = SchemaLoader.load_profile("test_profile", profiles_dir=tmp_path)
        assert [c.name for c in cols] == ["a", "b"]

        # Pin to 1.0.0: only a
        cols = SchemaLoader.load_profile(
            "test_profile", profiles_dir=tmp_path, profile_version="1.0.0"
        )
        assert [c.name for c in cols] == ["a"]

    def test_shipped_profiles_have_version_tree(self):
        """All shipped profiles should have current marker and version tree."""
        for profile in ("timeseries", "minimal", "passthrough"):
            meta = SchemaLoader.load_version_metadata(_resolve_profiles_dir() / f"{profile}.yaml")
            assert "current" in meta, f"Profile '{profile}' missing 'current' marker"
            assert "versions" in meta, f"Profile '{profile}' missing 'versions' dict"

    def test_each_version_has_date(self):
        """Each version entry should have a date."""
        for profile in ("timeseries", "minimal", "passthrough"):
            meta = SchemaLoader.load_version_metadata(_resolve_profiles_dir() / f"{profile}.yaml")
            for ver, entry in meta["versions"].items():
                assert "date" in entry, f"Version '{ver}' in profile '{profile}' missing 'date'"


# ── load_version_metadata ──────────────────────────────────────────


class TestLoadVersionMetadata:
    def test_versioned_file(self, tmp_versioned_schema):
        path = tmp_versioned_schema(
            {
                "current": "1.2.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "summary": "Initial",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                    "1.2.0": {
                        "date": "2026-03-02",
                        "type": "addition",
                        "summary": "Added field",
                        "columns": [
                            {"name": "a", "type": "string"},
                            {"name": "b", "type": "string"},
                        ],
                    },
                },
            }
        )
        meta = SchemaLoader.load_version_metadata(path)
        assert meta["current"] == "1.2.0"
        assert "1.0.0" in meta["versions"]
        assert "1.2.0" in meta["versions"]
        assert meta["versions"]["1.2.0"]["type"] == "addition"
        # columns should be stripped from metadata output
        assert "columns" not in meta["versions"]["1.0.0"]
        assert "columns" not in meta["versions"]["1.2.0"]

    def test_unversioned_file(self, tmp_schema):
        path = tmp_schema([{"name": "x", "type": "string"}])
        meta = SchemaLoader.load_version_metadata(path)
        assert meta == {}

    def test_file_not_found(self):
        with pytest.raises(SchemaLoadError, match="not found"):
            SchemaLoader.load_version_metadata("/nonexistent/path.yaml")


# ── load_profile_exclude ────────────────────────────────────────────


class TestLoadProfileExclude:
    def _schema(self, tmp_versioned_schema, exclude=None):
        entry = {
            "date": "2026-08-05",
            "type": "revision",
            "summary": "s",
            "columns": [{"name": "a", "type": "string"}],
        }
        if exclude is not None:
            entry["profile_exclude"] = exclude
        return tmp_versioned_schema({"current": "1.0.1", "versions": {"1.0.1": entry}})

    def test_reads_current_version(self, tmp_versioned_schema):
        path = self._schema(tmp_versioned_schema, ["_raw", "_tags"])
        assert SchemaLoader.load_profile_exclude(path) == ["_raw", "_tags"]

    def test_absent_field_is_empty(self, tmp_versioned_schema):
        path = self._schema(tmp_versioned_schema)
        assert SchemaLoader.load_profile_exclude(path) == []

    def test_explicit_version(self, tmp_versioned_schema):
        path = tmp_versioned_schema(
            {
                "current": "1.0.1",
                "versions": {
                    "1.0.0": {
                        "date": "2026-06-10",
                        "type": "model",
                        "summary": "s",
                        "columns": [{"name": "a", "type": "string"}],
                    },
                    "1.0.1": {
                        "date": "2026-08-05",
                        "type": "revision",
                        "summary": "s",
                        "profile_exclude": ["_raw"],
                        "columns": [{"name": "a", "type": "string"}],
                    },
                },
            }
        )
        assert SchemaLoader.load_profile_exclude(path, "1.0.0") == []
        assert SchemaLoader.load_profile_exclude(path, "1.0.1") == ["_raw"]

    def test_unversioned_file_is_empty(self, tmp_schema):
        path = tmp_schema([{"name": "x", "type": "string"}])
        assert SchemaLoader.load_profile_exclude(path) == []
