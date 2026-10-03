#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_derived_registry_dual_roots.py
#  Purpose:      list and get resolve the deploy repo and the shipped tree
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""With gitops on, list and get must see what the build can already resolve.

``resolve_derived_reference`` checks the deploy repo first and the shipped
schemas tree second. The registry's read methods follow the same order, so a
source can never bind a derived schema the API reports as missing.
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.derived import DerivedSchema
from dfe_engine.schema.derived_registry import (
    DerivedSchemaNotFoundError,
    DerivedSchemaRegistry,
    derived_reference,
    shipped_derived_directory,
)
from dfe_engine.settings import DFESettings
from dfe_engine.yaml_utils import yaml_dump

PATH = "beats/filebeat_auth"


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


def _schema() -> DerivedSchema:
    return DerivedSchema.model_validate({**_doc(), "path": derived_reference(PATH)})


def _settings(tmp_path):
    return DFESettings(
        env="dev",
        schemas={"schemas_dir": str(tmp_path / "schemas")},
        gitops={"enabled": True, "local_path": str(tmp_path / "deploy")},
    )


def _registry(settings) -> DerivedSchemaRegistry:
    repo = GitopsRepo(local_path=settings.gitops.local_path, push=False)
    return DerivedSchemaRegistry.from_settings(settings, crud=GitCrud(repo, default_registry()))


def _write_shipped(settings, key: str, **version_keys) -> None:
    """Drop a YAML file straight into the shipped tree, as a release would."""
    root = shipped_derived_directory(settings)
    assert root is not None
    path = root / f"{key}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_dump(_doc(**version_keys), path)


class TestList:
    def test_a_shipped_only_schema_is_listed_with_origin_shipped(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, "beats/shipped_only")
        registry = _registry(settings)
        rows = registry.list()
        assert [(row["path"], row["origin"]) for row in rows] == [
            ("derived/beats/shipped_only", "shipped")
        ]

    def test_a_name_in_both_roots_is_listed_once_from_the_deploy_repo(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, PATH)
        registry = _registry(settings)
        registry.save(_schema(), created_by="tester <tester@dfe.local>")
        rows = registry.list()
        assert len(rows) == 1
        assert rows[0]["origin"] == "deploy"


class TestGet:
    def test_falls_back_to_the_shipped_tree(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, "beats/shipped_only")
        registry = _registry(settings)
        schema = registry.get("beats/shipped_only")
        assert schema.origin == "shipped"

    def test_the_deploy_repo_wins_a_name_clash(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, PATH, select=[{"name": "message"}])
        registry = _registry(settings)
        registry.save(_schema(), created_by="tester <tester@dfe.local>")
        schema = registry.get(PATH)
        assert schema.origin == "deploy"
        assert [entry.name for entry in schema.version().select] == ["timestamp", "host_name"]


class TestWritePathIsUnchanged:
    """A shipped-only name is invisible to exists(), save() and delete().

    list and get read both roots; the write path does not grow new semantics
    here. The API's create route gates on exists(), so it still treats a
    shipped-only name as absent and writes the deploy-repo copy; its update
    and delete routes gate on exists()/delete() the same way, so they still
    answer not-found for a name only the shipped tree carries.
    """

    def test_exists_does_not_see_a_shipped_only_name(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, "beats/shipped_only")
        registry = _registry(settings)
        assert registry.exists("beats/shipped_only") is False

    def test_saving_a_shipped_only_name_creates_the_deploy_copy(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, "beats/shipped_only")
        registry = _registry(settings)
        registry.save(
            DerivedSchema.model_validate(
                {**_doc(), "path": derived_reference("beats/shipped_only")}
            ),
            created_by="tester <tester@dfe.local>",
        )
        assert registry.get("beats/shipped_only").origin == "deploy"

    def test_deleting_a_shipped_only_name_is_not_found(self, tmp_path):
        settings = _settings(tmp_path)
        _write_shipped(settings, "beats/shipped_only")
        registry = _registry(settings)
        with pytest.raises(DerivedSchemaNotFoundError):
            registry.delete("beats/shipped_only")
