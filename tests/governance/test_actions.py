#  Project:      dfe-engine
#  File:         tests/governance/test_actions.py
#  Purpose:      Tests for defined-action store + atomic invoke + policy enforcement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Defined actions over a real local gitops repo (no mocks)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import (
    ActionDef,
    ActionStore,
    PolicyStore,
    ProtectedPolicy,
    ProtectedVarError,
    VarChange,
)
from dfe_engine.governance.policies import _POLICY_CLASS


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry(
        [
            ResourceClass("helmvars", "values", rbac_prefix="helmvars"),
            ResourceClass("actions", "governance/actions", rbac_prefix="governance"),
            ResourceClass("policies", "governance/policies", rbac_prefix="governance"),
        ]
    )
    return GitCrud(repo, registry)


@pytest.fixture
def store(crud):
    return ActionStore(crud)


def _scale_action():
    return ActionDef(
        name="scale-receiver",
        description="Scale the receiver",
        required_action="action:invoke:scale-receiver",
        changes=[
            VarChange(cls="helmvars", name="receiver-default", path="replicaCount", value=3),
            VarChange(cls="helmvars", name="receiver-default", path="keda.maxReplicas", value=10),
        ],
    )


def test_action_store_crud_round_trips(store):
    store.save(_scale_action(), actor="admin")
    assert store.list() == ["scale-receiver"]
    got = store.get("scale-receiver")
    assert got.required_action == "action:invoke:scale-receiver"
    assert len(got.changes) == 2
    store.delete("scale-receiver", actor="admin")
    assert store.list() == []


def test_invoke_applies_all_changes_in_one_commit(store, crud):
    store.save(_scale_action(), actor="admin")
    res = store.invoke("scale-receiver", actor="bob")
    assert res.changed is True
    assert res.commit_sha
    doc = crud.get("helmvars", "receiver-default")
    assert doc["replicaCount"] == 3
    assert doc["keda"]["maxReplicas"] == 10
    # diff captured old (None) -> new
    by_path = {d["path"]: d for d in res.diff}
    assert by_path["replicaCount"]["old"] is None
    assert by_path["replicaCount"]["new"] == 3


def test_dry_run_does_not_commit(store, crud):
    store.save(_scale_action(), actor="admin")
    res = store.invoke("scale-receiver", actor="bob", dry_run=True)
    assert res.dry_run is True
    assert res.commit_sha is None
    assert len(res.diff) == 2
    # nothing written
    from dfe_engine.gitcrud import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "receiver-default")


def test_invoke_blocked_by_protected_policy_is_atomic(store, crud):
    # lock replicaCount on every helmvars resource
    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(name="lockdown", protected=["helmvars:*:replicaCount"]).model_dump(),
        actor="admin",
    )
    store.save(_scale_action(), actor="admin")
    policy = PolicyStore(crud)
    with pytest.raises(ProtectedVarError):
        store.invoke("scale-receiver", actor="bob", policy=policy)
    # atomic: the second (unprotected) change must NOT have landed either
    from dfe_engine.gitcrud import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "receiver-default")


def test_invoke_protected_allowed_with_override(store, crud):
    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(name="lockdown", protected=["helmvars:*:replicaCount"]).model_dump(),
        actor="admin",
    )
    store.save(_scale_action(), actor="admin")
    policy = PolicyStore(crud)
    res = store.invoke("scale-receiver", actor="bob", policy=policy, override=True)
    assert res.changed is True
    assert crud.get("helmvars", "receiver-default")["replicaCount"] == 3
