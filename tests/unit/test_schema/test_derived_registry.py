#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_derived_registry.py
#  Purpose:      Tests for the derived-schema document, its store and its capture mapping
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A derived schema stored, read back, and compiled into a loader capture mode.

Both backends are exercised against the same assertions, because the choice
between the deploy repo and the schemas tree is a deployment's, not the
model's.
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitPolicyError
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.derived import (
    CAPTURE_MODE_EXTRACTED_ONLY,
    CAPTURE_MODE_FULL,
    CAPTURE_MODE_RAW_ONLY,
    DerivedSchema,
    IncompatibleIndexError,
    UnknownIndexUseCaseError,
    UnknownSelectionError,
    capture_mode,
    validate_against_base,
)
from dfe_engine.schema.derived_registry import (
    DerivedSchemaNotFoundError,
    DerivedSchemaRegistry,
    canonical_derived_path,
    derived_reference,
    derived_reference_root,
)
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.settings import DFESettings
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

PATH = "beats/filebeat_auth"

BASE_COLUMNS = [
    SchemaColumn(name="timestamp", type="timestamp", use_case="range"),
    SchemaColumn(name="host_name", type="string", use_case="dimension", expr="@source: host.name"),
    SchemaColumn(name="message", type="text"),
    SchemaColumn(name="log_offset", type="integer", use_case="range"),
]


def _doc(**version_keys) -> dict:
    block = {
        "date": "2026-09-21",
        "summary": "system.auth subset",
        "select": [{"name": "timestamp"}, {"name": "host_name", "index": "exact_match"}],
        **version_keys,
    }
    return {
        "base": "meta/beats/filebeat",
        "base_version": "1.0.0",
        "current": "1.0.0",
        "versions": {"1.0.0": block},
    }


def _schema(**version_keys) -> DerivedSchema:
    return DerivedSchema.model_validate({**_doc(**version_keys), "path": derived_reference(PATH)})


@pytest.fixture
def directory_registry(tmp_path) -> DerivedSchemaRegistry:
    return DerivedSchemaRegistry(tmp_path / "schemas" / "derived")


@pytest.fixture
def gitcrud_registry(tmp_path) -> DerivedSchemaRegistry:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return DerivedSchemaRegistry(crud=GitCrud(repo, default_registry()))


@pytest.fixture(params=["directory", "gitcrud"])
def registry(request, directory_registry, gitcrud_registry) -> DerivedSchemaRegistry:
    return directory_registry if request.param == "directory" else gitcrud_registry


class TestDocument:
    def test_a_base_with_a_file_extension_is_refused(self):
        # The UI and the schemas list both speak registry paths; a suffix here
        # would be a second spelling of one reference.
        with pytest.raises(ValueError, match="without a file extension"):
            DerivedSchema.model_validate({**_doc(), "base": "meta/beats/filebeat.yaml"})

    def test_current_must_name_a_version(self):
        with pytest.raises(ValueError, match="is not defined in versions"):
            DerivedSchema.model_validate({**_doc(), "current": "9.9.9"})

    def test_a_select_entry_may_set_nothing_but_index(self):
        with pytest.raises(ValueError):
            DerivedSchema.model_validate(_doc(select=[{"name": "host_name", "type": "integer"}]))

    def test_capture_json_without_capture_raw_is_refused(self):
        # No dfe-loader capture mode populates _json and leaves _raw empty.
        with pytest.raises(ValueError, match="dfe-loader cannot express"):
            DerivedSchema.model_validate(_doc(capture_json=True, capture_raw=False))

    def test_the_safety_net_is_on_unless_asked(self):
        version = _schema().version()
        assert (version.capture_json, version.capture_raw) == (True, True)

    def test_defaults_stay_off_disk(self):
        stored = _schema().to_yaml_dict()["versions"]["1.0.0"]
        assert "capture_json" not in stored
        assert stored["select"] == [
            {"name": "timestamp"},
            {"name": "host_name", "index": "exact_match"},
        ]


class TestValidationAgainstBase:
    def test_a_name_the_base_does_not_define_is_refused(self):
        schema = _schema(select=[{"name": "nope"}])
        with pytest.raises(UnknownSelectionError) as exc:
            validate_against_base(schema, BASE_COLUMNS, registry=TypeRegistry.default())
        assert exc.value.column == "nope"
        assert exc.value.base == "meta/beats/filebeat"

    def test_an_index_the_registry_does_not_know_is_refused(self):
        schema = _schema(select=[{"name": "message", "index": "fulltext"}])
        with pytest.raises(UnknownIndexUseCaseError) as exc:
            validate_against_base(schema, BASE_COLUMNS, registry=TypeRegistry.default())
        assert "word_search" in exc.value.valid

    def test_an_index_the_primitive_cannot_take_is_refused(self):
        schema = _schema(select=[{"name": "log_offset", "index": "word_search"}])
        with pytest.raises(IncompatibleIndexError) as exc:
            validate_against_base(schema, BASE_COLUMNS, registry=TypeRegistry.default())
        assert exc.value.primitive == "integer"

    def test_index_none_needs_no_registry_support(self):
        schema = _schema(select=[{"name": "log_offset", "index": "none"}])
        validate_against_base(schema, BASE_COLUMNS, registry=TypeRegistry.default())

    def test_every_version_is_checked_not_just_current(self):
        schema = DerivedSchema.model_validate(
            {
                **_doc(),
                "versions": {
                    "1.0.0": _doc()["versions"]["1.0.0"],
                    "2.0.0": {
                        "date": "2026-09-22",
                        "summary": "later",
                        "select": [{"name": "nope"}],
                    },
                },
            }
        )
        with pytest.raises(UnknownSelectionError):
            validate_against_base(
                schema, BASE_COLUMNS, registry=TypeRegistry.default(), version_id="2.0.0"
            )


class TestPaths:
    def test_the_reference_form_and_the_bare_key_are_one_resource(self):
        assert canonical_derived_path("derived/beats/x") == "beats/x"
        assert canonical_derived_path("beats/x") == "beats/x"
        assert derived_reference("beats/x") == "derived/beats/x"

    def test_a_traversing_segment_is_refused(self):
        with pytest.raises(Exception, match="Invalid path segment"):
            canonical_derived_path("beats/../../etc")

    def test_the_reference_root_is_one_level_above_the_documents(self, tmp_path):
        settings = DFESettings(env="dev", schemas={"schemas_dir": str(tmp_path)})
        registry = DerivedSchemaRegistry.from_settings(settings)
        assert registry.reference_root == derived_reference_root(settings)
        assert registry.directory == tmp_path / "derived"

    def test_gitops_puts_them_in_the_deploy_repo(self, tmp_path):
        settings = DFESettings(
            env="dev",
            schemas={"schemas_dir": str(tmp_path)},
            gitops={"enabled": True, "local_path": str(tmp_path / "deploy")},
        )
        assert derived_reference_root(settings) == tmp_path / "deploy" / "config" / "schemas"


class TestStore:
    def test_a_saved_schema_reads_back(self, registry):
        registry.save(_schema(), created_by="tester <tester@dfe.local>")
        read = registry.get(PATH)
        assert read.base == "meta/beats/filebeat"
        assert read.path == "derived/beats/filebeat_auth"
        assert [entry.name for entry in read.version().select] == ["timestamp", "host_name"]

    def test_the_reference_form_reads_the_same_resource(self, registry):
        registry.save(_schema(), created_by="tester <tester@dfe.local>")
        assert registry.get("derived/beats/filebeat_auth").current == "1.0.0"

    def test_a_missing_schema_is_not_found(self, registry):
        with pytest.raises(DerivedSchemaNotFoundError):
            registry.get("beats/absent")

    def test_the_listing_carries_the_group(self, registry):
        registry.save(_schema(), created_by="tester <tester@dfe.local>")
        rows = registry.list()
        assert [row["path"] for row in rows] == ["derived/beats/filebeat_auth"]
        assert rows[0]["column_count"] == 2

    def test_delete_removes_it(self, registry):
        registry.save(_schema(), created_by="tester <tester@dfe.local>")
        registry.delete(PATH, created_by="tester <tester@dfe.local>")
        assert registry.exists(PATH) is False
        with pytest.raises(DerivedSchemaNotFoundError):
            registry.delete(PATH)

    def test_the_stored_file_is_what_the_schema_loader_reads(self, registry, tmp_path):
        # The API writes the document and the DDL build reads it: assert the two
        # meet on one file rather than trusting the path convention.
        registry.save(_schema(capture_json=False, capture_raw=False))
        resolved = registry.reference_root / "derived" / f"{PATH}.yaml"
        assert resolved.is_file()
        columns = SchemaLoader.apply_derived_schema(BASE_COLUMNS, resolved)
        assert [c.name for c in columns] == ["timestamp", "host_name"]
        assert columns[1].use_case == "exact_match"
        # The base's directive survives the selection, because dfe-loader reads
        # it back out of the ClickHouse column comment.
        assert columns[1].expr == "@source: host.name"
        assert SchemaLoader.load_derived_capture(resolved) == {
            "capture_json": False,
            "capture_raw": False,
        }


class TestGitcrudNesting:
    def test_a_nested_name_lands_in_a_directory_tree(self, gitcrud_registry):
        gitcrud_registry.save(_schema(), created_by="tester <tester@dfe.local>")
        stored = gitcrud_registry.directory / "beats" / "filebeat_auth.yaml"
        assert stored.is_file()

    def test_a_traversing_segment_is_still_refused_by_the_name_rule(self, tmp_path):
        repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
        crud = GitCrud(repo, default_registry())
        with pytest.raises(CommitPolicyError):
            crud.get("derived_schemas", "beats/../../etc/passwd")

    def test_a_flat_class_still_refuses_a_slash(self, tmp_path):
        repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
        crud = GitCrud(repo, default_registry())
        with pytest.raises(CommitPolicyError):
            crud.get("sources", "beats/filebeat")


class TestCaptureMode:
    @pytest.mark.parametrize(
        ("capture_json", "capture_raw", "expected"),
        [
            (True, True, CAPTURE_MODE_FULL),
            (False, True, CAPTURE_MODE_RAW_ONLY),
            (False, False, CAPTURE_MODE_EXTRACTED_ONLY),
        ],
    )
    def test_the_pair_maps_to_one_loader_mode(self, capture_json, capture_raw, expected):
        assert capture_mode(capture_json=capture_json, capture_raw=capture_raw) == expected
