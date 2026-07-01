#  Project:      dfe-engine
#  File:         tests/gitcrud/test_versioned.py
#  Purpose:      VersionedDoc - draft/publish/rollback over a real local gitops repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The per-artifact versioning authority, against a real local git repo (no mocks)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import (
    GitCrud,
    ResourceClass,
    ResourceClassRegistry,
    VersionedDoc,
)
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def vc(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    reg = ResourceClassRegistry(
        [ResourceClass("rules", "rules", rbac_prefix="rules", versioned=True)]
    )
    return VersionedDoc(GitCrud(repo, reg))


def test_draft_publish_rollback_lifecycle(vc):
    vc.save_draft("rules", "brute-force", {"sql": "SELECT 1"}, actor="kaz")
    assert vc.get_draft("rules", "brute-force")["sql"] == "SELECT 1"
    assert vc.get_published("rules", "brute-force") is None  # nothing published yet
    assert vc.status("rules", "brute-force") == "draft"

    assert vc.publish("rules", "brute-force", actor="kaz") == 1
    assert vc.get_published("rules", "brute-force")["sql"] == "SELECT 1"
    assert vc.status("rules", "brute-force") == "published"

    vc.save_draft("rules", "brute-force", {"sql": "SELECT 2"}, actor="kaz")
    assert vc.publish("rules", "brute-force", actor="kaz") == 2
    assert vc.list_versions("rules", "brute-force") == [1, 2]
    assert vc.get_version("rules", "brute-force", 1)["sql"] == "SELECT 1"
    assert vc.get_published("rules", "brute-force")["sql"] == "SELECT 2"

    # rollback points current back to v1 but NEVER destroys v2
    vc.rollback("rules", "brute-force", 1, actor="kaz")
    assert vc.get_published("rules", "brute-force")["sql"] == "SELECT 1"
    assert vc.list_versions("rules", "brute-force") == [1, 2]


def test_flat_doc_loads_as_implicit_v1(vc):
    # a pre-versioning flat doc (raw write) reads as one published version
    vc._crud.put("rules", "legacy", {"sql": "SELECT 9"}, actor="x")
    assert vc.get_published("rules", "legacy")["sql"] == "SELECT 9"
    assert vc.list_versions("rules", "legacy") == [1]


def test_published_snapshots_are_immutable(vc):
    vc.save_draft("rules", "r", {"sql": "a"}, actor="x")
    vc.publish("rules", "r", actor="x")
    vc.save_draft("rules", "r", {"sql": "b"}, actor="x")  # a new draft must not touch v1
    assert vc.get_version("rules", "r", 1)["sql"] == "a"
    assert vc.get_draft("rules", "r")["sql"] == "b"


def test_publish_with_no_draft_raises(vc):
    with pytest.raises(ValueError):
        vc.publish("rules", "never-drafted", actor="x")


def test_rollback_to_missing_version_raises(vc):
    vc.save_draft("rules", "r", {"sql": "a"}, actor="x")
    vc.publish("rules", "r", actor="x")
    with pytest.raises(ValueError):
        vc.rollback("rules", "r", 99, actor="x")


def test_deployed_pointer_tracks_reconciled_version(vc):
    vc.save_draft("rules", "r", {"sql": "a"}, actor="x")
    vc.publish("rules", "r", actor="x")
    vc.save_draft("rules", "r", {"sql": "b"}, actor="x")
    vc.publish("rules", "r", actor="x")
    assert vc.deployed("rules", "r") is None  # nothing reconciled yet
    vc.set_deployed("rules", "r", 1, actor="argo")
    # current is v2 (latest published) but only v1 is deployed - commit != deployment
    assert vc.get_published("rules", "r")["sql"] == "b"
    assert vc.deployed("rules", "r") == 1


def test_each_op_is_a_commit(vc):
    # head_revision() is git's own commit id (we only read it); each op advances it
    vc.save_draft("rules", "r", {"sql": "a"}, actor="x")
    rev_after_draft = vc._crud.head_revision()
    vc.publish("rules", "r", actor="x")
    rev_after_publish = vc._crud.head_revision()
    assert rev_after_draft
    assert rev_after_publish
    assert rev_after_draft != rev_after_publish
