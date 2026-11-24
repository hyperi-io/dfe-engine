"""Tests for the schema version edits and SchemaManager's column validation."""

import pytest

from dfe_engine.schema.schema_loader import SchemaLoader, SchemaLoadError
from dfe_engine.schema.schema_manager import (
    SchemaManager,
    SchemaVersionError,
    add_version_in,
    delete_version_in,
    fork_document,
    next_version_for_type,
    set_current_in,
    set_summary_in,
)
from dfe_engine.source.models import SchemaColumn
from dfe_engine.yaml_utils import yaml_dump, yaml_load

# -- Fixtures -------------------------------------------------------


def _apply(edit, path, *args, **kwargs):
    """Run one document edit against the YAML file at *path*, as the registry does."""
    document = yaml_load(path)
    edit(document, *args, origin=str(path), **kwargs)
    yaml_dump(document, path)


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
                    {"name": "message", "type": "text", "use_case": "substring_search"},
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


# -- TestAddVersion -------------------------------------------------


class TestNextVersionForType:
    def test_model_bump(self):
        assert next_version_for_type("1.2.3", "model") == "2.0.0"

    def test_addition_bump(self):
        assert next_version_for_type("1.2.3", "addition") == "1.3.0"

    def test_revision_bump(self):
        assert next_version_for_type("1.2.3", "revision") == "1.2.4"

    def test_rejects_non_semver_current(self):
        with pytest.raises(SchemaVersionError, match=r"x\.x\.x"):
            next_version_for_type("1", "addition")


class TestSetCurrent:
    def test_set_current(self, multi_version_schema):
        _apply(set_current_in, multi_version_schema, "1.0.0")
        data = yaml_load(multi_version_schema)
        assert data["current"] == "1.0.0"

    def test_set_current_unknown_version(self, multi_version_schema):
        with pytest.raises(SchemaVersionError, match="not found"):
            _apply(set_current_in, multi_version_schema, "9.9.9")


class TestUpdateVersionSummary:
    def test_updates_summary_only(self, versioned_schema):
        _apply(set_summary_in, versioned_schema, "1.0.0", "Revised summary")
        data = yaml_load(versioned_schema)
        assert data["versions"]["1.0.0"]["summary"] == "Revised summary"
        assert data["versions"]["1.0.0"]["type"] == "model"


class TestDeleteVersion:
    def test_deletes_non_current_version(self, multi_version_schema):
        _apply(delete_version_in, multi_version_schema, "1.0.0")
        data = yaml_load(multi_version_schema)
        assert "1.0.0" not in data["versions"]
        assert "1.1.0" in data["versions"]
        assert data["current"] == "1.1.0"

    def test_repoints_current_when_deleting_current(self, multi_version_schema):
        _apply(delete_version_in, multi_version_schema, "1.1.0")
        data = yaml_load(multi_version_schema)
        assert "1.1.0" not in data["versions"]
        assert data["current"] == "1.0.0"

    def test_rejects_unknown_version(self, multi_version_schema):
        with pytest.raises(SchemaVersionError, match="not found"):
            _apply(delete_version_in, multi_version_schema, "9.9.9")

    def test_rejects_last_remaining_version(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="last remaining"):
            _apply(delete_version_in, versioned_schema, "1.0.0")


class TestAddVersion:
    def test_add_version_to_existing(self, versioned_schema):
        new_cols = [
            {"name": "event_time", "type": "datetime", "use_case": "range", "order": 0},
            {"name": "org_id", "type": "string", "attribute": ["lowcardinality"]},
            {"name": "message", "type": "text", "use_case": "substring_search"},
            {"name": "severity", "type": "string", "attribute": ["lowcardinality"]},
        ]
        _apply(
            add_version_in,
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

    def test_add_version_persists_ch_override(self, versioned_schema):
        from dfe_engine.schema.models import SchemaColumnWrite

        col = SchemaColumnWrite.model_validate(
            {
                "name": "event_id",
                "type": "datetime",
                "_field_type": "user_defined",
                "ch_override": "DateTime",
                "expr": "@source: EventId",
            }
        )
        _apply(
            add_version_in,
            versioned_schema,
            "1.2.0",
            [col.to_yaml_dict()],
            type="model",
            summary="With ch_override",
        )
        data = yaml_load(versioned_schema)
        written = data["versions"]["1.2.0"]["columns"][0]
        assert written["ch_override"] == "DateTime"

        loaded = SchemaLoader.load_columns(versioned_schema, version="1.2.0")
        assert loaded[0].ch_override == "DateTime"

    def test_set_current_true(self, versioned_schema):
        _apply(
            add_version_in,
            versioned_schema,
            "2.0.0",
            [{"name": "ts", "type": "datetime"}],
            type="model",
            set_current=True,
        )
        data = yaml_load(versioned_schema)
        assert data["current"] == "2.0.0"

    def test_set_current_false(self, versioned_schema):
        _apply(
            add_version_in,
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
            _apply(
                add_version_in,
                versioned_schema,
                "1.0.0",
                [{"name": "ts", "type": "datetime"}],
            )

    def test_refuse_empty_columns(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="columns must contain at least one column"):
            _apply(add_version_in, versioned_schema, "1.1.0", [])

    def test_validates_columns(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="validation failed"):
            _apply(
                add_version_in,
                versioned_schema,
                "1.1.0",
                [{"name": "bad_col", "type": "nonexistent_type"}],
            )

    def test_skip_validation(self, versioned_schema):
        # Should not raise even with invalid type
        _apply(
            add_version_in,
            versioned_schema,
            "1.1.0",
            [{"name": "col", "type": "nonexistent_type"}],
            validate=False,
        )
        data = yaml_load(versioned_schema)
        assert "1.1.0" in data["versions"]

    def test_preserves_existing_versions(self, versioned_schema):
        _apply(
            add_version_in,
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
        _apply(add_version_in, versioned_schema, "2.0.0", cols)
        loaded = SchemaLoader.load_columns(versioned_schema, version="2.0.0")
        assert len(loaded) == 2

    def test_auto_generates_date(self, versioned_schema):
        _apply(
            add_version_in,
            versioned_schema,
            "1.1.0",
            [{"name": "ts", "type": "datetime"}],
        )
        data = yaml_load(versioned_schema)
        assert "date" in data["versions"]["1.1.0"]
        assert len(data["versions"]["1.1.0"]["date"]) == 10  # YYYY-MM-DD


# -- TestValidateMetaSchemaColumns ----------------------------------


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


# -- TestForkDocument -----------------------------------------------


class TestForkDocument:
    def test_fork_carries_the_current_version_as_a_custom_schema(self, multi_version_schema):
        fork = fork_document(yaml_load(multi_version_schema), summary="Forked", origin="core")

        assert fork["resource_type"] == "custom"
        assert fork["current"] == "1.1.0"
        assert list(fork["versions"]) == ["1.1.0"]
        # current is 1.1.0 which has 3 columns including new_col
        assert [c["name"] for c in fork["versions"]["1.1.0"]["columns"]] == [
            "ts",
            "old_col",
            "new_col",
        ]
        assert fork["versions"]["1.1.0"]["summary"] == "Forked"

    def test_fork_of_an_explicit_version(self, multi_version_schema):
        fork = fork_document(
            yaml_load(multi_version_schema), source_version="1.0.0", summary="Forked", origin="core"
        )

        assert fork["current"] == "1.0.0"
        assert len(fork["versions"]["1.0.0"]["columns"]) == 2  # 1.0.0 only has 2 columns

    def test_fork_leaves_the_source_untouched(self, versioned_schema):
        source = yaml_load(versioned_schema)

        fork_document(source, summary="Forked", origin="core")

        assert source == yaml_load(versioned_schema)

    def test_unknown_source_version_is_refused(self, versioned_schema):
        with pytest.raises(SchemaVersionError, match="not found"):
            fork_document(
                yaml_load(versioned_schema), source_version="9.9.9", summary="Forked", origin="core"
            )

    def test_a_document_with_no_version_tree_is_refused(self):
        with pytest.raises(SchemaLoadError, match="must be a mapping"):
            fork_document({"columns": []}, summary="Forked", origin="core")


# -- Integration ----------------------------------------------------


class TestIntegration:
    def test_fork_then_modify_schema(self, versioned_schema, tmp_path):
        """Fork a meta schema, then evolve the fork independently."""
        fork = tmp_path / "fork.yaml"
        yaml_dump(fork_document(yaml_load(versioned_schema), summary="Forked", origin="test"), fork)

        _apply(
            add_version_in,
            fork,
            "1.1.0",
            [
                *yaml_load(fork)["versions"]["1.0.0"]["columns"],
                {"name": "extra", "type": "integer"},
            ],
            summary="Added extra",
        )

        # The fork has 2 versions with the new one current, and the source still has 1
        assert len(SchemaLoader.load_version_metadata(fork)["versions"]) == 2
        assert SchemaLoader.load_version_metadata(fork)["current"] == "1.1.0"
        assert len(SchemaLoader.load_columns(fork, version="1.1.0")) == 4
        assert len(SchemaLoader.load_version_metadata(versioned_schema)["versions"]) == 1


class TestRetiredUseCaseWriteBack:
    """A schema stored before the vocabulary rename saves back under the current name.

    Reads translate a retired name either way. Without the write-back the old word
    rides forward into every version derived from the one that carried it, so the
    retired word never leaves a live gitops repo (dfe-engine#468).
    """

    @pytest.fixture
    def retired_schema(self, tmp_path):
        path = tmp_path / "retired.yaml"
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "summary": "Stored before the rename",
                        "columns": [
                            {"name": "event_time", "type": "datetime", "use_case": "range"},
                            {"name": "message", "type": "text", "use_case": "fulltext"},
                            {"name": "trace_id", "type": "uuid", "use_case": "bloom"},
                        ],
                    }
                },
            },
            path,
        )
        return path

    def test_a_fork_of_a_retired_version_carries_the_current_names(self, retired_schema):
        fork = fork_document(
            yaml_load(retired_schema), source_version="1.0.0", summary="Forked", origin="test"
        )

        written = fork["versions"]["1.0.0"]["columns"]
        by_name = {col["name"]: col for col in written}

        assert by_name["message"]["use_case"] == "word_search"
        assert by_name["trace_id"]["use_case"] == "exact_match"
        assert by_name["event_time"]["use_case"] == "range"

    def test_the_version_that_was_stored_is_left_as_it_was(self, retired_schema):
        """The rewrite lands on the version being written, never on history."""
        source = yaml_load(retired_schema)

        fork_document(source, source_version="1.0.0", summary="Forked", origin="test")

        stored = source["versions"]["1.0.0"]["columns"]

        assert [col["name"] for col in stored if col.get("use_case") == "fulltext"] == ["message"]

    def test_a_new_version_written_from_retired_dicts_lands_current(self, retired_schema):
        _apply(
            add_version_in,
            retired_schema,
            "2.0.0",
            [{"name": "body", "type": "text", "use_case": "text_search", "_field_type": "base"}],
            type="model",
        )

        written = yaml_load(retired_schema)["versions"]["2.0.0"]["columns"]

        assert written[0]["use_case"] == "substring_search"

    def test_a_column_declaring_no_use_case_does_not_gain_a_null_one(self, tmp_path):
        path = tmp_path / "plain.yaml"
        yaml_dump(
            {
                "current": "1.0.0",
                "versions": {
                    "1.0.0": {
                        "date": "2026-01-15",
                        "type": "model",
                        "summary": "No use cases",
                        "columns": [{"name": "proc_id", "type": "integer"}],
                    }
                },
            },
            path,
        )

        fork = fork_document(
            yaml_load(path), source_version="1.0.0", summary="Forked", origin=str(path)
        )

        written = fork["versions"]["1.0.0"]["columns"]

        assert "use_case" not in written[0]
