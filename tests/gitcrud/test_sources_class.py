#  Project:      dfe-engine
#  File:         tests/gitcrud/test_sources_class.py
#  Purpose:      Tests for the gitcrud-backed SourceRegistry (sources class)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""SourceRegistry over the gitcrud `sources` class (real dulwich repo, no mocks).

The all-in-one source-definition YAML IS the gitcrud doc: API payload =
exchange file = stored doc, every mutation is one commit, and the universal
gitcrud metadata block rides on the stored file.
"""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.metadata import extract_metadata
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.source.models import Source, SourceMatch, SourceView
from dfe_engine.source.registry import (
    SourceMatchConflictError,
    SourceNotFoundError,
    SourceRegistry,
)
from dfe_engine.yaml_utils import yaml_load


@pytest.fixture
def crud(tmp_path):
    """GitCrud over a fresh local (no-remote) deploy repo, default registry."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo)


@pytest.fixture
def registry(crud):
    return SourceRegistry(crud=crud)


def _source(name: str = "filebeat", *, value: str | None = None, enabled: bool = True) -> Source:
    return Source(
        source=name,
        enabled=enabled,
        description=f"{name} test source",
        match=SourceMatch(field="tags.collector.type", value=value or name),
        views=[SourceView(standard="sigma", taxonomy="linux")],
    )


class TestCrudBackedCrud:
    def test_save_and_get_round_trip(self, registry):
        registry.save_source(_source())
        loaded = registry.get_source("filebeat")
        assert loaded.source == "filebeat"
        assert loaded.view_for("sigma").taxonomy == "linux"

    def test_doc_lands_in_config_sources(self, registry, crud):
        registry.save_source(_source())
        path = crud.repo_path / "config" / "sources" / "filebeat.yaml"
        assert path.is_file()

    def test_stored_doc_carries_metadata_block(self, registry, crud):
        registry.save_source(_source())
        doc = yaml_load(crud.repo_path / "config" / "sources" / "filebeat.yaml")
        meta = extract_metadata(doc)
        assert meta.description == "filebeat test source"
        assert meta.display_name == "Filebeat"
        # the metadata block does not break model round-trip (extra ignored)
        assert Source.model_validate(doc).source == "filebeat"

    def test_each_save_is_one_commit(self, registry, crud):
        before = crud.head_revision()
        registry.save_source(_source())
        mid = crud.head_revision()
        assert mid != before
        registry.save_source(_source("syslog"))
        assert crud.head_revision() != mid

    def test_get_missing_raises(self, registry):
        with pytest.raises(SourceNotFoundError):
            registry.get_source("nope")

    def test_source_exists(self, registry):
        assert registry.source_exists("filebeat") is False
        registry.save_source(_source())
        assert registry.source_exists("filebeat") is True

    def test_list_sources_summary(self, registry):
        registry.save_source(_source())
        rows = registry.list_sources()
        assert len(rows) == 1
        assert rows[0]["source"] == "filebeat"
        assert rows[0]["views"] == ["sigma"]

    def test_delete_source_commits_removal(self, registry, crud):
        registry.save_source(_source())
        registry.delete_source("filebeat")
        assert registry.source_exists("filebeat") is False
        assert not (crud.repo_path / "config" / "sources" / "filebeat.yaml").exists()

    def test_delete_missing_is_noop(self, registry):
        registry.delete_source("nope")  # warns, does not raise

    def test_delete_commit_carries_attribution(self, registry, crud):
        """Delete commits are attributed like saves: '(by <created_by>)' suffix."""
        from dulwich.repo import Repo

        registry.save_source(_source())
        registry.delete_source("filebeat", created_by="kaz")
        repo = Repo(str(crud.repo_path))
        msg = repo[repo.head()].message.decode()
        assert msg.startswith("source: delete filebeat")
        assert "(by kaz)" in msg

    def test_versioned_doc_save_draft_refuses_self_managed_envelope(self, registry, crud):
        """A sources doc carries semver version keys owned by the Source model -
        VersionedDoc.save_draft must refuse it with the clear error instead of
        writing a mixed draft/semver envelope."""
        from dfe_engine.gitcrud.versioned import VersionedDoc

        registry.save_source(_source())
        with pytest.raises(ValueError, match="self-managed"):
            VersionedDoc(crud).save_draft("sources", "filebeat", {"x": 1}, actor="kaz")

    def test_match_conflict_enforced_across_docs(self, registry):
        registry.save_source(_source("alpha", value="shared"))
        with pytest.raises(SourceMatchConflictError):
            registry.save_source(_source("beta", value="shared"))

    def test_is_git_true(self, registry):
        assert registry.is_git is True
        assert registry.current_branch == "main"


class TestCrudSeedBuiltins:
    def test_seed_lands_one_commit(self, registry, crud):
        before = crud.head_revision()
        count = registry.seed_builtin_sources()
        assert count >= 1
        after = crud.head_revision()
        assert after != before
        assert registry.source_exists("dfe_alerts")
        # ALL seeds in the one commit: exactly one new commit on the branch
        registry.get_source("dfe_alerts")

    def test_seed_idempotent(self, registry, crud):
        registry.seed_builtin_sources()
        head = crud.head_revision()
        assert registry.seed_builtin_sources(overwrite=False) == 0
        assert crud.head_revision() == head
