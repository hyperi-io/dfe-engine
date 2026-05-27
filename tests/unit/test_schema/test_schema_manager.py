"""Tests for SchemaManager — schema version management write operations."""

import pytest

from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError
from dfe_engine.schema.schema_manager import (
    SchemaManager,
    SchemaVersionError,
    next_version_for_type,
)
from dfe_engine.source.models import SchemaColumn
from dfe_engine.yaml_utils import yaml_dump, yaml_load

# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def versioned_schema(tmp_path):
    """Create a versioned schema file with a single 1.0.0 version."""
    path = tmp_path / "test_schema.yaml"
    data = {
        "current": "1.0.0",
        "versions": {
            "1.0.0": {
                "date": "2026-01-15",
                "type": "model",
                "summary": "Initial schema",
                "columns": [
                    {"name": "event_time", "type": "datetime", "use_case": "range", "order": 0},
                    {"name": "org_id", "type": "string", "attribute": ["lowcardinality"]},
                    {"name": "message", "type": "text", "use_case": "text_search"},
                ],
            }
        },
    }
    yaml_dump(data, path)
    return path


@pytest.fixture
def multi_version_schema(tmp_path):
    """Schema with two versions."""
    path = tmp_path / "multi.yaml"
    data = {
        "current": "1.1.0",
        "versions": {
            "1.0.0": {
                "date": "2026-01-15",
                "type": "model",
                "summary": "Initial",
                "columns": [
                    {"name": "ts", "type": "datetime"},
                    {"name": "old_col", "type": "string"},
                ],
            },
            "1.1.0": {
                "date": "2026-02-01",
                "type": "addition",
                "summary": "Added new_col",
                "columns": [
                    {"name": "ts", "type": "datetime"},
                    {"name": "old_col", "type": "string"},
                    {"name": "new_col", "type": "integer"},
                ],
            },
        },
    }
    yaml_dump(data, path)
    return path


# ── TestAddVersion ─────────────────────────────────────────────────


class TestNextVersionForType:
    def test_model_bump(self):
        assert next_version_for_type("1.2.3", "model") == "2.0.0"

    def test_addition_bump(self):
        assert next_version_for_type("1.2.3", "addition") == "1.3.0"

    def test_revision_bump(self):
        assert next_version_for_type("1.2.3", "revision") == "1.2.4"

    def test_rejects_non_semver_current(self):
        with pytest.raises(SchemaVersionError, match="x.x.x"):
            next_version_for_type("1", "addition")


class TestSetCurrent:
    def test_set_current(self, multi_version_schema):
        SchemaManager.set_current(multi_version_schema, "1.0.0")
        data = yaml_load(multi_version_schema)
        assert data["current"] == "1.0.0"

    def test_set_current_unknown_version(self, multi_version_schema):
        with pytest.raises(SchemaVersionError, match="not found"):
            SchemaManager.set_current(multi_version_schema, "9.9.9")


class TestUpdateVersionSummary:
    def test_updates_summary_only(self, versioned_schema):
        SchemaManager.update_version_summary(versioned_schema, "1.0.0", "Revised summary")
        data = yaml_load(versioned_schema)
        assert data["versions"]["1.0.0"]["summary"] == "Revised summary"
        assert data["versions"]["1.0.0"]["type"] == "model"


class TestAddVersion:
    def test_add_version_to_existing(self, versioned_schema):
        new_cols = [
            {"name": "event_time", "type": "datetime", "use_case": "range", "order": 0},
            {"name": "org_id", "type": "string", "attribute": ["lowcardinality"]},
            {"name": "message", "type": "text", "use_case": "text_search"},
            {"name": "severity", "type": "string", "attribute": ["lowcardinality"]},
        ]
        SchemaManager.add_version(
            versioned_schema,
            "1.1.0",
            new_cols,
            type="addition",
            summary="Added severity",
        )
        # Verify version was added
        meta = SchemaLoader.load_version_metadata(versioned_schema)
        assert "1.1.0" in meta["versions"]
        assert meta["current"] == "1.1.0"

        # Verify columns loadable
        cols = SchemaLoader.load_columns(versioned_schema, version="1.1.0")
        assert len(cols) == 4
        assert cols[-1].name == "severity"

    def test_set_current_true(self, versioned_schema):
        SchemaManager.add_version(
            versioned_schema,
            "2.0.0",
            [{"name": "ts", "type": "datetime"}],
            type="model",
            set_current=True,
        )
        data = yaml_load(versioned_schema)
        assert data["current"] == "2.0.0"

    def test_set_current_false(self, versioned_schema):
        SchemaManager.add_version(
            versioned_schema,
            "2.0.0",
            [{"name": "ts", "type": "datetime"}],
            type="model",
            set_current=False,
        )
        data = yaml_load(versioned_schema)
        assert data["current"] == "1.0.0"  # Unchanged

    def test_refuse_duplicate_version(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="already exists"):
            SchemaManager.add_version(
                versioned_schema,
                "1.0.0",
                [{"name": "ts", "type": "datetime"}],
            )

    def test_validates_columns(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="validation failed"):
            SchemaManager.add_version(
                versioned_schema,
                "1.1.0",
                [{"name": "bad_col", "type": "nonexistent_type"}],
            )

    def test_skip_validation(self, versioned_schema):
        # Should not raise even with invalid type
        SchemaManager.add_version(
            versioned_schema,
            "1.1.0",
            [{"name": "col", "type": "nonexistent_type"}],
            validate=False,
        )
        data = yaml_load(versioned_schema)
        assert "1.1.0" in data["versions"]

    def test_preserves_existing_versions(self, versioned_schema):
        SchemaManager.add_version(
            versioned_schema,
            "1.1.0",
            [{"name": "ts", "type": "datetime"}],
        )
        # Original version still intact
        cols = SchemaLoader.load_columns(versioned_schema, version="1.0.0")
        assert len(cols) == 3
        assert cols[0].name == "event_time"

    def test_accepts_schema_column_objects(self, versioned_schema):
        cols = [
            SchemaColumn(name="ts", type="datetime"),
            SchemaColumn(name="user", type="string"),
        ]
        SchemaManager.add_version(versioned_schema, "2.0.0", cols)
        loaded = SchemaLoader.load_columns(versioned_schema, version="2.0.0")
        assert len(loaded) == 2

    def test_auto_generates_date(self, versioned_schema):
        SchemaManager.add_version(
            versioned_schema,
            "1.1.0",
            [{"name": "ts", "type": "datetime"}],
        )
        data = yaml_load(versioned_schema)
        assert "date" in data["versions"]["1.1.0"]
        assert len(data["versions"]["1.1.0"]["date"]) == 10  # YYYY-MM-DD

    def test_file_not_found(self, tmp_path):
        with pytest.raises(SchemaLoadError, match="not found"):
            SchemaManager.add_version(
                tmp_path / "missing.yaml",
                "1.0.0",
                [{"name": "ts", "type": "datetime"}],
            )


# ── TestCloneVersion ───────────────────────────────────────────────


class TestCloneVersion:
    def test_clone_from_explicit_version(self, versioned_schema):
        result = SchemaManager.clone_version(
            versioned_schema,
            "2.0.0",
            source_version="1.0.0",
            type="model",
            summary="Cloned from 1.0.0",
        )
        assert len(result) == 3
        assert result[0].name == "event_time"

        # Verify in file
        cols = SchemaLoader.load_columns(versioned_schema, version="2.0.0")
        assert len(cols) == 3

    def test_clone_from_current(self, multi_version_schema):
        result = SchemaManager.clone_version(
            multi_version_schema,
            "2.0.0",
            type="model",
            summary="Major revision",
        )
        # current is 1.1.0 which has 3 columns
        assert len(result) == 3
        names = [c.name for c in result]
        assert "new_col" in names

    def test_refuse_duplicate_new_version(self, multi_version_schema):
        with pytest.raises(SchemaVersionError, match="already exists"):
            SchemaManager.clone_version(
                multi_version_schema,
                "1.0.0",
                source_version="1.1.0",
            )

    def test_source_version_not_found(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="not found"):
            SchemaManager.clone_version(
                versioned_schema,
                "2.0.0",
                source_version="9.9.9",
            )

    def test_modification_add(self, versioned_schema):
        result = SchemaManager.clone_version(
            versioned_schema,
            "1.1.0",
            source_version="1.0.0",
            column_modifications=[
                {"action": "add", "column": {"name": "severity", "type": "string"}},
            ],
        )
        assert len(result) == 4
        assert result[-1].name == "severity"

    def test_modification_remove(self, versioned_schema):
        result = SchemaManager.clone_version(
            versioned_schema,
            "2.0.0",
            source_version="1.0.0",
            type="model",
            column_modifications=[
                {"action": "remove", "name": "message"},
            ],
        )
        assert len(result) == 2
        names = [c.name for c in result]
        assert "message" not in names

    def test_modification_update(self, versioned_schema):
        result = SchemaManager.clone_version(
            versioned_schema,
            "2.0.0",
            source_version="1.0.0",
            type="model",
            column_modifications=[
                {"action": "update", "name": "message", "column": {"type": "string"}},
            ],
        )
        msg_col = next(c for c in result if c.name == "message")
        assert msg_col.type == "string"  # Changed from text

    def test_modification_remove_nonexistent_raises(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="not found for removal"):
            SchemaManager.clone_version(
                versioned_schema,
                "2.0.0",
                source_version="1.0.0",
                column_modifications=[
                    {"action": "remove", "name": "nonexistent"},
                ],
            )

    def test_returns_schema_columns(self, versioned_schema):
        result = SchemaManager.clone_version(
            versioned_schema,
            "2.0.0",
            source_version="1.0.0",
        )
        assert all(isinstance(c, SchemaColumn) for c in result)


# ── TestValidateMetaSchemaColumns ──────────────────────────────────


class TestValidateMetaSchemaColumns:
    def test_accepts_valid_meta_schema(self):
        from dfe_engine.schema.models import MetaSchema, SchemaColumn, SchemaVersion

        meta = MetaSchema(
            current="1",
            path="test/schema",
            versions={
                "1": SchemaVersion(
                    date="2026-01-01",
                    type="model",
                    summary="init",
                    columns=[SchemaColumn(name="e", type="string", expr="@source: E")],
                )
            },
        )
        SchemaManager.validate_meta_schema_columns(meta)

    def test_rejects_invalid_column_type(self):
        from dfe_engine.schema.models import MetaSchema, SchemaColumn, SchemaVersion

        meta = MetaSchema(
            current="1",
            path="test/schema",
            versions={
                "1": SchemaVersion(
                    date="2026-01-01",
                    type="model",
                    summary="init",
                    columns=[SchemaColumn(name="bad", type="not_a_type", expr="@source: X")],
                )
            },
        )
        with pytest.raises(SchemaVersionError, match="Version '1'"):
            SchemaManager.validate_meta_schema_columns(meta)


# ── TestCreateMetaSchema ───────────────────────────────────────────


class TestCreateMetaSchema:
    def test_create_new_file(self, tmp_path):
        path = tmp_path / "new_schema.yaml"
        SchemaManager.create_meta_schema(
            path,
            [
                {"name": "ts", "type": "datetime", "order": 0},
                {"name": "user_id", "type": "string"},
            ],
            summary="New source schema",
        )
        assert path.exists()

        # Verify loadable by SchemaLoader
        cols = SchemaLoader.load_columns(path)
        assert len(cols) == 2
        assert cols[0].name == "ts"

    def test_refuses_existing_file(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="already exists"):
            SchemaManager.create_meta_schema(
                versioned_schema,
                [{"name": "ts", "type": "datetime"}],
            )

    def test_validates_columns(self, tmp_path):
        with pytest.raises(SchemaVersionError, match="validation failed"):
            SchemaManager.create_meta_schema(
                tmp_path / "bad.yaml",
                [{"name": "bad", "type": "not_a_type"}],
            )

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "schema.yaml"
        SchemaManager.create_meta_schema(
            path,
            [{"name": "ts", "type": "datetime"}],
        )
        assert path.exists()

    def test_version_tree_format(self, tmp_path):
        path = tmp_path / "schema.yaml"
        SchemaManager.create_meta_schema(
            path,
            [{"name": "ts", "type": "datetime"}],
            initial_version="0.1.0",
            type="model",
            summary="Alpha",
        )
        data = yaml_load(path)
        assert data["current"] == "0.1.0"
        assert "0.1.0" in data["versions"]
        ver = data["versions"]["0.1.0"]
        assert ver["type"] == "model"
        assert ver["summary"] == "Alpha"
        assert len(ver["columns"]) == 1


# ── TestCloneMetaSchema ────────────────────────────────────────────


class TestCloneMetaSchema:
    def test_clone_creates_new_file(self, versioned_schema, tmp_path):
        dest = tmp_path / "cloned.yaml"
        SchemaManager.clone_meta_schema(versioned_schema, dest)
        assert dest.exists()

        cols = SchemaLoader.load_columns(dest)
        assert len(cols) == 3
        assert cols[0].name == "event_time"

    def test_clone_uses_current_version(self, multi_version_schema, tmp_path):
        dest = tmp_path / "cloned.yaml"
        SchemaManager.clone_meta_schema(multi_version_schema, dest)
        cols = SchemaLoader.load_columns(dest)
        # current is 1.1.0 which has 3 columns including new_col
        assert len(cols) == 3
        names = [c.name for c in cols]
        assert "new_col" in names

    def test_clone_explicit_version(self, multi_version_schema, tmp_path):
        dest = tmp_path / "cloned.yaml"
        SchemaManager.clone_meta_schema(
            multi_version_schema,
            dest,
            source_version="1.0.0",
        )
        cols = SchemaLoader.load_columns(dest)
        assert len(cols) == 2  # 1.0.0 only has 2 columns

    def test_refuse_existing_dest(self, versioned_schema, tmp_path):
        dest = tmp_path / "existing.yaml"
        dest.write_text("existing content")
        with pytest.raises(SchemaVersionError, match="already exists"):
            SchemaManager.clone_meta_schema(versioned_schema, dest)

    def test_source_not_found(self, tmp_path):
        with pytest.raises(SchemaLoadError, match="not found"):
            SchemaManager.clone_meta_schema(
                tmp_path / "missing.yaml",
                tmp_path / "dest.yaml",
            )

    def test_default_summary(self, versioned_schema, tmp_path):
        dest = tmp_path / "cloned.yaml"
        SchemaManager.clone_meta_schema(versioned_schema, dest)
        data = yaml_load(dest)
        assert "Cloned from" in data["versions"]["1.0.0"]["summary"]


# ── Integration ────────────────────────────────────────────────────


class TestIntegration:
    def test_create_add_clone_lifecycle(self, tmp_path):
        path = tmp_path / "lifecycle.yaml"

        # Create initial schema
        SchemaManager.create_meta_schema(
            path,
            [
                {"name": "ts", "type": "datetime", "order": 0},
                {"name": "user", "type": "string"},
            ],
            initial_version="1.0.0",
            summary="Initial",
        )

        # Add a second version with extra column
        SchemaManager.add_version(
            path,
            "1.1.0",
            [
                {"name": "ts", "type": "datetime", "order": 0},
                {"name": "user", "type": "string"},
                {"name": "action", "type": "string", "attribute": ["lowcardinality"]},
            ],
            type="addition",
            summary="Added action",
        )

        # Clone v1.1.0 to v2.0.0, removing user column
        result = SchemaManager.clone_version(
            path,
            "2.0.0",
            source_version="1.1.0",
            type="model",
            summary="Removed user",
            column_modifications=[{"action": "remove", "name": "user"}],
        )
        assert len(result) == 2

        # All three versions are loadable
        v1 = SchemaLoader.load_columns(path, version="1.0.0")
        v11 = SchemaLoader.load_columns(path, version="1.1.0")
        v2 = SchemaLoader.load_columns(path, version="2.0.0")
        assert len(v1) == 2
        assert len(v11) == 3
        assert len(v2) == 2

        # Current is 2.0.0
        meta = SchemaLoader.load_version_metadata(path)
        assert meta["current"] == "2.0.0"

    def test_clone_then_modify_schema(self, versioned_schema, tmp_path):
        """Clone a meta schema, then evolve the clone independently."""
        clone = tmp_path / "clone.yaml"
        SchemaManager.clone_meta_schema(versioned_schema, clone)

        # Add a version to the clone
        SchemaManager.clone_version(
            clone,
            "1.1.0",
            source_version="1.0.0",
            column_modifications=[
                {"action": "add", "column": {"name": "extra", "type": "integer"}},
            ],
        )

        # Clone has 2 versions, source still has 1
        clone_meta = SchemaLoader.load_version_metadata(clone)
        source_meta = SchemaLoader.load_version_metadata(versioned_schema)
        assert len(clone_meta["versions"]) == 2
        assert len(source_meta["versions"]) == 1
