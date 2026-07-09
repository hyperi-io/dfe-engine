#  Project:      dfe-engine
#  File:         tests/gitcrud/test_engine.py
#  Purpose:      Tests for the generic YAML-in-git CRUD engine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for GitCrud over a real local dulwich repo (no mocks)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import (
    GitCrud,
    ResourceClass,
    ResourceClassRegistry,
    ResourceNotFoundError,
    flatten,
)
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path):
    """GitCrud over a fresh local (no-remote) git repo."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry(
        [
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            ResourceClass("governance", "governance", rbac_prefix="governance"),
        ]
    )
    return GitCrud(repo, registry)


def test_flatten_produces_dot_paths():
    doc = {
        "deploy": {"service": "receiver"},
        "config": {"kafka": {"brokers": "b:9092"}},
        "replicaCount": 1,
        "topics": ["a", "b"],
    }
    flat = flatten(doc)
    assert flat["config.kafka.brokers"] == "b:9092"
    assert flat["replicaCount"] == 1
    assert flat["deploy.service"] == "receiver"
    assert flat["topics[0]"] == "a"
    assert flat["topics[1]"] == "b"


def test_put_then_get_round_trips_and_commits(crud):
    res = crud.put(
        "helmvars",
        "receiver-default",
        {"deploy": {"service": "receiver"}, "replicaCount": 1},
        actor="alice",
    )
    assert res.changed is True
    assert res.commit_sha
    doc = crud.get("helmvars", "receiver-default")
    assert doc["replicaCount"] == 1
    assert doc["deploy"]["service"] == "receiver"


def test_set_key_creates_intermediates_and_commits(crud):
    res = crud.set_key("helmvars", "receiver-default", "replicaCount", 3, actor="bob")
    assert res.changed is True
    assert res.commit_sha
    # nested path that did not exist before -> intermediates created
    crud.set_key("helmvars", "receiver-default", "keda.maxReplicas", 10, actor="bob")
    doc = crud.get("helmvars", "receiver-default")
    assert doc["replicaCount"] == 3
    assert doc["keda"]["maxReplicas"] == 10


def test_delete_key_reverts(crud):
    crud.set_key("helmvars", "receiver-default", "replicaCount", 3, actor="bob")
    crud.delete_key("helmvars", "receiver-default", "replicaCount", actor="bob")
    doc = crud.get("helmvars", "receiver-default")
    assert "replicaCount" not in doc


def test_list_returns_names(crud):
    crud.put("helmvars", "receiver-default", {"a": 1}, actor="x")
    crud.put("helmvars", "loader-default", {"a": 1}, actor="x")
    crud.put("governance", "role-admin", {"a": 1}, actor="x")
    assert crud.list("helmvars") == ["loader-default", "receiver-default"]
    assert crud.list("governance") == ["role-admin"]


def test_list_empty_class_is_empty(crud):
    assert crud.list("governance") == []


def test_vars_flattens_a_resource(crud):
    crud.put(
        "helmvars",
        "receiver-default",
        {"config": {"kafka": {"brokers": "b:9092"}}},
        actor="x",
    )
    assert crud.vars("helmvars", "receiver-default")["config.kafka.brokers"] == "b:9092"


def test_get_missing_raises(crud):
    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "nope")


def test_delete_removes_resource(crud):
    crud.put("helmvars", "receiver-default", {"a": 1}, actor="x")
    res = crud.delete("helmvars", "receiver-default", actor="x")
    assert res.changed
    assert crud.list("helmvars") == []
    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "receiver-default")


def test_unchanged_put_does_not_commit(crud):
    crud.put("helmvars", "receiver-default", {"a": 1}, actor="x")
    again = crud.put("helmvars", "receiver-default", {"a": 1}, actor="x")
    assert again.changed is False
