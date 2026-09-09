#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_seed.py
#  Purpose:      Tests for seeding the library from mounted content
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Startup seeding over a real git repo, run the way a deployment runs it.

The seeder runs on EVERY boot, and a deployment restarts for reasons that have
nothing to do with content, so the property that matters is not "it seeds" but
"a second run costs nothing and an operator's own version survives it".
"""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import library, seed
from dfe_engine.appmgmt.links import LIBRARY_CLASS
from dfe_engine.gitcrud.engine import ResourceNotFoundError

VECTOR_TEMPLATE = "sources:\n  dfe_source:\n    type: kafka\n"
VECTOR_TEMPLATE_V2 = "sources:\n  dfe_source:\n    type: kafka\n    topics: [land]\n"
VRL_PIPELINE = ". = parse_json!(.message)\n"


@pytest.fixture
def mount(tmp_path):
    """A content mount carrying one file under each of two kinds."""
    root = tmp_path / "content" / "library"
    (root / "vector-yaml").mkdir(parents=True)
    (root / "vrl").mkdir(parents=True)
    (root / "vector-yaml" / "bus.yaml").write_text(VECTOR_TEMPLATE, encoding="utf-8")
    (root / "vrl" / "filebeat.vrl").write_text(VRL_PIPELINE, encoding="utf-8")
    return root


def summary(crud, name: str) -> library.ArtifactSummary:
    return library.summarise(name, crud.get(LIBRARY_CLASS, name))


class TestFirstRun:
    def test_every_mounted_file_becomes_an_artefact(self, crud, mount):
        changed = seed.seed_library(crud, mount)
        assert sorted(crud.list(LIBRARY_CLASS)) == ["bus", "filebeat"]
        assert len(changed) == 4  # two publishes plus two tags

    def test_the_kind_comes_from_the_directory(self, crud, mount):
        seed.seed_library(crud, mount)
        assert summary(crud, "bus").kind == "vector-yaml"
        assert summary(crud, "filebeat").kind == "vrl"

    def test_shipped_content_is_marked_as_shipped(self, crud, mount):
        # The console distinguishes shipped from authored on these two, and an
        # operator lists one without the other by them.
        seed.seed_library(crud, mount)
        found = summary(crud, "bus")
        assert found.group == seed.SEED_GROUP
        assert found.labels == seed.SEED_ORIGIN_LABEL

    def test_the_content_lands_as_a_real_file_in_the_bundle(self, crud, mount):
        seed.seed_library(crud, mount)
        assert crud.read_payload(LIBRARY_CLASS, "bus", "current.yaml") == VECTOR_TEMPLATE
        assert crud.read_payload(LIBRARY_CLASS, "filebeat", "current.vrl") == VRL_PIPELINE

    def test_the_stack_tag_names_the_published_version(self, crud, mount):
        seed.seed_library(crud, mount)
        assert summary(crud, "bus").tags == {seed.SEED_TAG: 1}

    def test_the_engine_is_the_actor(self, crud, mount):
        seed.seed_library(crud, mount)
        doc = crud.get(LIBRARY_CLASS, "bus")
        assert library.read_version(doc, 1).published_by == seed.SEED_ACTOR


class TestSecondRun:
    def test_an_unchanged_mount_writes_nothing(self, crud, mount):
        seed.seed_library(crud, mount)
        head = crud.head_revision()
        assert seed.seed_library(crud, mount) == []
        assert crud.head_revision() == head

    def test_moved_content_publishes_the_next_version_and_moves_the_tag(self, crud, mount):
        seed.seed_library(crud, mount)
        (mount / "vector-yaml" / "bus.yaml").write_text(VECTOR_TEMPLATE_V2, encoding="utf-8")

        changed = seed.seed_library(crud, mount)
        found = summary(crud, "bus")
        assert found.versions == (1, 2)
        assert found.tags == {seed.SEED_TAG: 2}
        assert found.current == 2
        assert changed == ["bus: published v2", "bus: stack -> v2"]

    def test_a_rolled_back_pin_brings_the_tag_back(self, crud, mount):
        # The version carrying the pinned content already exists, so the pointer
        # moves and no third version is appended for content the series holds.
        seed.seed_library(crud, mount)
        (mount / "vector-yaml" / "bus.yaml").write_text(VECTOR_TEMPLATE_V2, encoding="utf-8")
        seed.seed_library(crud, mount)
        (mount / "vector-yaml" / "bus.yaml").write_text(VECTOR_TEMPLATE, encoding="utf-8")

        assert seed.seed_library(crud, mount) == ["bus: stack -> v1"]
        found = summary(crud, "bus")
        assert found.versions == (1, 2)
        assert found.tags == {seed.SEED_TAG: 1}


class TestAnOperatorsOwnVersion:
    def test_a_later_authored_version_survives_the_next_boot(self, crud, mount):
        seed.seed_library(crud, mount)
        doc = crud.get(LIBRARY_CLASS, "bus")
        _, _, plan = library.publish(doc, content="# mine\n", actor="alice")
        crud.put_bundle(
            LIBRARY_CLASS, "bus", doc, "alice", writes=dict(plan.writes), message="cfg(bus): mine"
        )

        assert seed.seed_library(crud, mount) == []
        found = summary(crud, "bus")
        assert found.current == 2, "the seed took `current` off the authored version"
        assert found.versions == (1, 2)
        assert crud.read_payload(LIBRARY_CLASS, "bus", "current.yaml") == "# mine\n"


class TestNothingToSeed:
    def test_no_directory_configured_is_a_no_op(self, crud):
        assert seed.seed_library(crud, None) == []

    def test_an_absent_directory_is_a_no_op(self, crud, tmp_path):
        assert seed.seed_library(crud, tmp_path / "nothing-here") == []

    def test_an_empty_directory_is_a_no_op(self, crud, tmp_path):
        (tmp_path / "empty").mkdir()
        assert seed.seed_library(crud, tmp_path / "empty") == []
        assert crud.list(LIBRARY_CLASS) == []


class TestContentThePinsShouldNotCarry:
    def test_an_undeclared_kind_directory_is_skipped(self, crud, mount):
        (mount / "lolcode").mkdir()
        (mount / "lolcode" / "hai.lol").write_text("HAI\n", encoding="utf-8")
        seed.seed_library(crud, mount)
        assert sorted(crud.list(LIBRARY_CLASS)) == ["bus", "filebeat"]

    def test_a_suffix_the_kind_does_not_claim_is_skipped(self, crud, mount):
        (mount / "vrl" / "README.md").write_text("notes\n", encoding="utf-8")
        seed.seed_library(crud, mount)
        with pytest.raises(ResourceNotFoundError):
            crud.get(LIBRARY_CLASS, "README")

    def test_one_unusable_file_does_not_cost_the_others_their_seeding(self, crud, mount):
        # A stem outside the resource-name character set is refused by the write
        # path, and the pin is the only thing that could carry a better name.
        (mount / "vrl" / "not a name.vrl").write_text("bad\n", encoding="utf-8")
        seed.seed_library(crud, mount)
        assert sorted(crud.list(LIBRARY_CLASS)) == ["bus", "filebeat"]


class TestWhereTheDirectoryComesFrom:
    def test_the_environment_names_it(self, crud, monkeypatch, tmp_path):
        monkeypatch.setenv(seed.SEED_DIR_ENV, str(tmp_path / "mounted"))
        assert seed.seed_dir() == tmp_path / "mounted"

    def test_unset_means_this_deployment_mounts_none(self, monkeypatch):
        monkeypatch.delenv(seed.SEED_DIR_ENV, raising=False)
        assert seed.seed_dir() is None
