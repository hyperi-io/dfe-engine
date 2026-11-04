#  Project:      dfe-engine
#  File:         tests/gitcrud/test_concurrency.py
#  Purpose:      Tests for put_many (atomic) + optimistic concurrency
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Atomic multi-resource commits + version-token concurrency (real local repo)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import (
    ConcurrencyConflictError,
    GitCrud,
    Layout,
    ResourceClass,
    ResourceClassRegistry,
)
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry(
        [
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            ResourceClass("blobs", "config/blobs", rbac_prefix="blobs", layout=Layout.BUNDLE),
        ]
    )
    return GitCrud(repo, registry)


def test_put_many_is_one_commit(crud):
    res = crud.put_many(
        [
            ("helmvars", "receiver-default", {"replicaCount": 3}),
            ("helmvars", "loader-default", {"replicaCount": 2}),
        ],
        actor="alice",
        message="action(scale): bump receiver+loader",
    )
    assert res.changed is True
    assert res.commit_sha
    # both landed in the SAME commit
    assert crud.get("helmvars", "receiver-default")["replicaCount"] == 3
    assert crud.get("helmvars", "loader-default")["replicaCount"] == 2
    assert len(res.files) == 2


def test_head_revision_advances_on_write(crud):
    assert crud.head_revision() is None  # empty repo
    crud.put("helmvars", "a", {"x": 1}, actor="x")
    r1 = crud.head_revision()
    assert r1
    crud.put("helmvars", "a", {"x": 2}, actor="x")
    r2 = crud.head_revision()
    assert r2 is not None
    assert r2 != r1


def test_put_with_current_base_revision_succeeds(crud):
    crud.put("helmvars", "a", {"x": 1}, actor="x")
    _doc, rev = crud.get_with_revision("helmvars", "a")
    res = crud.put("helmvars", "a", {"x": 2}, actor="x", base_revision=rev)
    assert res.changed is True


@pytest.mark.parametrize(
    ("cls", "name", "theirs", "write"),
    [
        pytest.param(
            "helmvars",
            "a",
            {"x": 99},
            lambda crud, rev: crud.put("helmvars", "a", {"x": 2}, "alice", base_revision=rev),
            id="put",
        ),
        pytest.param(
            "helmvars",
            "a",
            {"x": 99},
            lambda crud, rev: crud.set_key("helmvars", "a", "x", 2, "alice", base_revision=rev),
            id="set_key",
        ),
        pytest.param(
            "blobs",
            "b",
            {"x": 99},
            lambda crud, rev: crud.put_bundle("blobs", "b", {"x": 2}, "alice", base_revision=rev),
            id="put_bundle",
        ),
        pytest.param(
            "helmvars",
            "a",
            {"x": 99},
            lambda crud, rev: crud.delete("helmvars", "a", "alice", base_revision=rev),
            id="delete",
        ),
        pytest.param(
            "helmvars",
            "a",
            None,
            lambda crud, rev: crud.delete("helmvars", "a", "alice", base_revision=rev),
            id="delete_of_a_resource_already_gone",
        ),
    ],
)
def test_a_write_with_a_stale_base_revision_conflicts(crud, cls, name, theirs, write):
    crud.put("helmvars", "a", {"x": 1}, actor="alice")
    crud.put_bundle("blobs", "b", {"x": 1}, actor="alice")
    stale = crud.head_revision()
    if theirs is None:
        crud.delete(cls, name, actor="bob")
    elif cls == "blobs":
        crud.put_bundle(cls, name, theirs, actor="bob")
    else:
        crud.put(cls, name, theirs, actor="bob")
    head = crud.head_revision()

    with pytest.raises(ConcurrencyConflictError) as ei:
        write(crud, stale)

    assert ei.value.current == (theirs or {})
    assert ei.value.head == head
    assert crud.head_revision() == head
