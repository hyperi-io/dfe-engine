#  Project:      dfe-engine
#  File:         tests/governance/test_action_unset.py
#  Purpose:      An action value of {"$unset": true} removes its path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unset over a real local gitops repo (no mocks).

Removing a var hands it back to the layer beneath the overlay, so a dial such as a
KEDA ceiling can return to its tier's own value without naming a number.
"""

import pytest
from pydantic import ValidationError

from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import ActionDef, ActionStore, PolicyStore, ProtectedPolicy

RECEIVER = "dfe-receiver-default-values"
UNSET = {"$unset": True}


@pytest.fixture
def crud(tmp_path):
    return GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())


@pytest.fixture
def store(crud):
    return ActionStore(crud)


def _action(path="keda.maxReplicaCount", value=UNSET, params=None, name=RECEIVER):
    doc = {
        "name": "normal",
        "params": params or {},
        "changes": [{"cls": "helmvars", "name": name, "path": path, "value": value}],
    }
    return ActionDef.model_validate(doc)


DEPLOY = {"deploy": {"service": "dfe-receiver", "instance": "default"}}

# (label, stored overlay, path, overlay left, diff old, diff new)
UNSET_CASES = [
    (
        "a present key goes and its siblings stay",
        {**DEPLOY, "keda": {"maxReplicaCount": 20, "minReplicaCount": 2}},
        "keda.maxReplicaCount",
        {**DEPLOY, "keda": {"minReplicaCount": 2}},
        20,
        UNSET,
    ),
    (
        "an absent key is a no-op",
        {**DEPLOY, "keda": {"minReplicaCount": 2}},
        "keda.maxReplicaCount",
        {**DEPLOY, "keda": {"minReplicaCount": 2}},
        None,
        UNSET,
    ),
    (
        "an absent parent is a no-op",
        {**DEPLOY},
        "keda.maxReplicaCount",
        {**DEPLOY},
        None,
        UNSET,
    ),
    (
        "the only key in a map takes the emptied map with it",
        {**DEPLOY, "keda": {"maxReplicaCount": 20}},
        "keda.maxReplicaCount",
        {**DEPLOY},
        20,
        UNSET,
    ),
    (
        "every emptied ancestor goes, and the first non-empty one stays",
        {**DEPLOY, "a": {"b": {"c": 1}, "keep": 2}},
        "a.b.c",
        {**DEPLOY, "a": {"keep": 2}},
        1,
        UNSET,
    ),
    (
        "a scalar in the way is left alone",
        {**DEPLOY, "keda": 5},
        "keda.maxReplicaCount",
        {**DEPLOY, "keda": 5},
        None,
        UNSET,
    ),
]


@pytest.mark.parametrize(
    ("label", "stored", "path", "left", "old", "new"), UNSET_CASES, ids=[c[0] for c in UNSET_CASES]
)
def test_invoke_removes_the_path(store, crud, label, stored, path, left, old, new):
    crud.put("helmvars", RECEIVER, stored, actor="seed")
    store.save(_action(path=path), actor="admin")
    res = store.invoke("normal", "op")
    assert crud.get("helmvars", RECEIVER) == left
    assert (res.diff[0]["old"], res.diff[0]["new"]) == (old, new)
    assert res.changed is (stored != left)


def test_unsetting_in_a_file_that_does_not_exist_creates_nothing(store, crud):
    store.save(_action(), actor="admin")
    res = store.invoke("normal", "op")
    assert res.changed is False
    assert res.diff[0]["new"] == UNSET
    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", RECEIVER)


def test_a_dry_run_changes_nothing_and_reports_the_removal(store, crud):
    crud.put("helmvars", RECEIVER, {**DEPLOY, "keda": {"maxReplicaCount": 20}}, actor="seed")
    store.save(_action(), actor="admin")
    res = store.invoke("normal", "op", dry_run=True)
    assert res.diff == [
        {
            "cls": "helmvars",
            "name": RECEIVER,
            "path": "keda.maxReplicaCount",
            "old": 20,
            "new": UNSET,
        }
    ]
    assert crud.get("helmvars", RECEIVER)["keda"] == {"maxReplicaCount": 20}


def test_an_unset_can_be_one_branch_of_a_map(store, crud):
    crud.put("helmvars", RECEIVER, {**DEPLOY, "keda": {"maxReplicaCount": 5}}, actor="seed")
    params = {"level": {"type": "enum", "values": ["normal", "surge"], "default": "normal"}}
    value = {"$param": "level", "map": {"normal": UNSET, "surge": 20}}
    store.save(_action(value=value, params=params), actor="admin")
    store.invoke("normal", "op", params={"level": "surge"})
    assert crud.get("helmvars", RECEIVER)["keda"] == {"maxReplicaCount": 20}
    store.invoke("normal", "op", params={"level": "normal"})
    assert "keda" not in crud.get("helmvars", RECEIVER)


def test_preview_checks_an_unset_branch_and_finds_nothing_wrong(store, crud):
    crud.put("helmvars", RECEIVER, {**DEPLOY, "keda": {"maxReplicaCount": 5}}, actor="seed")
    params = {"level": {"type": "enum", "values": ["normal", "surge"], "default": "normal"}}
    value = {"$param": "level", "map": {"normal": UNSET, "surge": 20}}
    diff, errors = store.preview(_action(value=value, params=params))
    assert errors == []
    assert diff[0]["new"] == UNSET


def test_preview_still_reports_a_lock_on_the_removed_path(store, crud):
    crud.put(
        "policies",
        "lock",
        ProtectedPolicy(name="lock", protected=["helmvars:*:keda.*"]).model_dump(),
        actor="a",
    )
    diff, errors = store.preview(_action(), policy=PolicyStore(crud))
    assert any("protected" in e for e in errors), errors


# (label, value) for a marker the model must refuse
BAD_MARKERS = [
    ("false", {"$unset": False}),
    ("a truthy non-bool", {"$unset": 1}),
    ("a second key", {"$unset": True, "x": 1}),
]


@pytest.mark.parametrize(("label", "value"), BAD_MARKERS, ids=[c[0] for c in BAD_MARKERS])
def test_a_malformed_marker_is_refused_at_define_time(label, value):
    with pytest.raises(ValidationError):
        _action(value=value)
    params = {"level": {"type": "enum", "values": ["a", "b"], "default": "a"}}
    with pytest.raises(ValidationError):
        _action(value={"$param": "level", "map": {"a": value, "b": 1}}, params=params)


def test_removing_a_credential_path_stores_no_credential(store):
    path = "config.kafka.sasl.password"
    assert store.credential_changes(_action(path=path)) == []
    assert store.credential_changes(_action(path=path, value="hunter2")) != []
