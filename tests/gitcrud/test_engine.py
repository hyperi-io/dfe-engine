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
    get_path,
    set_path,
)
from dfe_engine.gitcrud.engine import _del_path
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


def _list_doc() -> dict:
    return {
        "tolerations": [{"key": "a", "value": 1}, {"key": "b"}],
        "matrix": [[1, 2], [3]],
        "plain": {"k": "v"},
    }


class TestListPathWalkers:
    """flatten() emits key[i] paths - the walkers must round-trip them."""

    def test_get_list_elements(self):
        doc = _list_doc()
        assert get_path(doc, "tolerations[0].key") == "a"
        assert get_path(doc, "tolerations[1].key") == "b"
        assert get_path(doc, "matrix[1][0]") == 3

    def test_get_out_of_range_returns_default(self):
        assert get_path(_list_doc(), "tolerations[9].key", "dflt") == "dflt"

    def test_get_index_into_non_list_returns_default(self):
        assert get_path(_list_doc(), "plain[0]", "dflt") == "dflt"

    def test_set_list_element_no_literal_key(self):
        doc = _list_doc()
        set_path(doc, "tolerations[0].key", "patched")
        assert doc["tolerations"][0]["key"] == "patched"
        assert "tolerations[0]" not in doc

    def test_set_nested_list_in_list(self):
        doc = _list_doc()
        set_path(doc, "matrix[0][1]", 99)
        assert doc["matrix"][0][1] == 99

    def test_set_out_of_range_raises(self):
        doc = _list_doc()
        with pytest.raises(ValueError, match=r"tolerations\[9\]"):
            set_path(doc, "tolerations[9].key", "x")
        assert "tolerations[9]" not in doc

    def test_set_index_into_non_list_raises(self):
        with pytest.raises(ValueError, match=r"plain\[0\]"):
            set_path(_list_doc(), "plain[0]", "x")

    def test_set_missing_list_raises(self):
        with pytest.raises(ValueError, match=r"nothere\[0\]"):
            set_path(_list_doc(), "nothere[0]", "x")

    def test_del_list_element(self):
        doc = _list_doc()
        assert _del_path(doc, "tolerations[0]") is True
        assert doc["tolerations"] == [{"key": "b"}]

    def test_del_nested_list_and_misses(self):
        doc = _list_doc()
        assert _del_path(doc, "matrix[0][1]") is True
        assert doc["matrix"] == [[1], [3]]
        assert _del_path(doc, "matrix[0][5]") is False
        assert _del_path(doc, "plain[0]") is False

    def test_del_key_inside_list_element(self):
        doc = _list_doc()
        assert _del_path(doc, "tolerations[0].value") is True
        assert doc["tolerations"][0] == {"key": "a"}

    def test_dict_paths_unchanged(self):
        doc = _list_doc()
        assert get_path(doc, "plain.k") == "v"
        set_path(doc, "plain.k2", 2)
        assert doc["plain"]["k2"] == 2
        assert _del_path(doc, "plain.k") is True


def test_set_key_list_path_edits_element_not_literal_key(crud):
    crud.put(
        "helmvars",
        "receiver-default",
        {"tolerations": [{"key": "a", "operator": "Exists"}]},
        actor="x",
    )
    res = crud.set_key("helmvars", "receiver-default", "tolerations[0].key", "b", actor="x")
    assert res.changed is True
    doc = crud.get("helmvars", "receiver-default")
    assert doc["tolerations"][0]["key"] == "b"
    assert doc["tolerations"][0]["operator"] == "Exists"
    assert "tolerations[0]" not in doc


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


@pytest.mark.parametrize(
    "bad", ["../evil", "../../etc/passwd", "sub/dir", "/abs", "..", ".", "a\x00b", "a\\b"]
)
def test_traversal_name_rejected(crud, bad):
    """A resource name is turned into directory/name.suffix - a name with a
    separator, parent-ref, absolute marker or NUL must be refused so a write cannot
    escape the class directory (F-GITCRUD-TRAVERSAL)."""
    with pytest.raises(ValueError):
        crud.put("helmvars", bad, {"a": 1}, actor="x")
    # nothing was written or committed for the bad name
    assert crud.list("helmvars") == []


def test_traversal_name_rejected_on_delete_and_get(crud):
    with pytest.raises(ValueError):
        crud.delete("helmvars", "../evil", actor="x")
    with pytest.raises(ValueError):
        crud.get("helmvars", "../evil")
    # a normal name is unaffected
    crud.put("helmvars", "receiver-default", {"a": 1}, actor="x")
    assert crud.list("helmvars") == ["receiver-default"]
