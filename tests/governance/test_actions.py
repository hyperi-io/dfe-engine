#  Project:      dfe-engine
#  File:         tests/governance/test_actions.py
#  Purpose:      Tests for defined-action store + atomic invoke + policy enforcement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Defined actions over a real local gitops repo (no mocks)."""

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
            ResourceClass("roles", "governance/rbac/roles", rbac_prefix="governance"),
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
            VarChange(cls="helmvars", name="receiver-default", path="keda.minReplicas", value=3),
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
    assert doc["keda"]["minReplicas"] == 3
    assert doc["keda"]["maxReplicas"] == 10
    # diff captured old (None) -> new
    by_path = {d["path"]: d for d in res.diff}
    assert by_path["keda.minReplicas"]["old"] is None
    assert by_path["keda.minReplicas"]["new"] == 3


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
    # lock keda.minReplicas on every helmvars resource
    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(name="lockdown", protected=["helmvars:*:keda.minReplicas"]).model_dump(),
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


def test_action_cannot_change_governance_class(store, crud):
    # SECURITY: an action that writes RBAC (governance class) is privilege escalation
    evil = ActionDef(
        name="escalate",
        description="sneaky",
        required_action="action:invoke:escalate",
        changes=[VarChange(cls="roles", name="admin", path="permissions", value=["*"])],
    )
    store.save(evil, actor="admin")
    from dfe_engine.governance import ActionForbiddenError

    with pytest.raises(ActionForbiddenError):
        store.invoke("escalate", actor="bob")
    # nothing was written
    from dfe_engine.gitcrud import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        crud.get("roles", "admin")


def test_action_change_rejected_by_commit_validators(store, crud):
    # an action cannot smuggle in a controller-owned field or a floating image
    from dfe_engine.gitcrud.commit_policy import CommitPolicyError

    bad = ActionDef(
        name="bad-scale",
        description="x",
        required_action="action:invoke:bad-scale",
        # replicaCount is controller-owned (KEDA) -> must be rejected by validate_change
        changes=[VarChange(cls="helmvars", name="receiver-default", path="replicaCount", value=3)],
    )
    store.save(bad, actor="admin")
    with pytest.raises(CommitPolicyError):
        store.invoke("bad-scale", actor="bob")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("image", {"repository": "ghcr.io/x/y", "tag": "latest"}),
        ("deploy", {"replicaCount": 3}),
        ("app", {"containers": [{"image": "ghcr.io/x/y:latest"}]}),
    ],
)
def test_a_map_change_cannot_carry_a_refused_leaf_even_with_the_override(store, crud, path, value):
    """The override lifts protected paths; the value rules still read every leaf of the map."""
    from dfe_engine.gitcrud.commit_policy import CommitPolicyError

    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(name="lockdown", protected=[f"helmvars:*:{path}"]).model_dump(),
        actor="admin",
    )
    action = ActionDef(
        name="reshape",
        description="x",
        required_action="action:invoke:reshape",
        changes=[
            VarChange(cls="helmvars", name="receiver-default", path="keda.maxReplicas", value=4),
            VarChange(cls="helmvars", name="receiver-default", path=path, value=value),
        ],
    )
    store.save(action, actor="admin")
    with pytest.raises(CommitPolicyError):
        store.invoke("reshape", actor="bob", policy=PolicyStore(crud), override=True)
    from dfe_engine.gitcrud import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "receiver-default")


def test_invoke_multi_file_is_atomic_one_commit(store, crud):
    # action touches TWO different files -> single commit, both applied
    action = ActionDef(
        name="scale-both",
        description="scale receiver + loader",
        required_action="action:invoke:scale-both",
        changes=[
            VarChange(cls="helmvars", name="receiver-default", path="keda.maxReplicas", value=10),
            VarChange(cls="helmvars", name="loader-default", path="keda.maxReplicas", value=5),
        ],
    )
    store.save(action, actor="admin")
    res = store.invoke("scale-both", actor="bob")
    assert res.changed is True
    assert crud.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 10
    assert crud.get("helmvars", "loader-default")["keda"]["maxReplicas"] == 5


def test_invoke_multi_file_protected_aborts_both(store, crud):
    # lock loader's var; the receiver change must NOT land either (atomic abort)
    crud.put(
        _POLICY_CLASS,
        "lock-loader",
        ProtectedPolicy(
            name="lock-loader", protected=["helmvars:loader-default:keda.maxReplicas"]
        ).model_dump(),
        actor="admin",
    )
    action = ActionDef(
        name="scale-both",
        description="x",
        required_action="action:invoke:scale-both",
        changes=[
            VarChange(cls="helmvars", name="receiver-default", path="keda.maxReplicas", value=10),
            VarChange(cls="helmvars", name="loader-default", path="keda.maxReplicas", value=5),
        ],
    )
    store.save(action, actor="admin")
    with pytest.raises(ProtectedVarError):
        store.invoke("scale-both", actor="bob", policy=PolicyStore(crud))
    from dfe_engine.gitcrud import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "receiver-default")


def test_a_parent_map_change_cannot_carry_a_locked_leaf_past_the_policy(store, crud):
    """The map lands at keda and replaces keda.minReplicas without naming it."""
    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(name="lockdown", protected=["helmvars:*:keda.minReplicas"]).model_dump(),
        actor="admin",
    )
    crud.put("helmvars", "receiver-default", {"keda": {"minReplicas": 2}}, actor="admin")
    action = ActionDef(
        name="reshape-keda",
        description="x",
        required_action="action:invoke:reshape-keda",
        changes=[
            VarChange(
                cls="helmvars",
                name="receiver-default",
                path="keda",
                value={"minReplicas": 0, "maxReplicas": 4},
            )
        ],
    )
    store.save(action, actor="admin")
    with pytest.raises(ProtectedVarError) as exc:
        store.invoke("reshape-keda", actor="bob", policy=PolicyStore(crud))
    assert exc.value.path == "keda"
    assert crud.get("helmvars", "receiver-default") == {"keda": {"minReplicas": 2}}


def test_invoke_protected_allowed_with_override(store, crud):
    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(name="lockdown", protected=["helmvars:*:keda.minReplicas"]).model_dump(),
        actor="admin",
    )
    store.save(_scale_action(), actor="admin")
    policy = PolicyStore(crud)
    res = store.invoke("scale-receiver", actor="bob", policy=policy, override=True)
    assert res.changed is True
    assert crud.get("helmvars", "receiver-default")["keda"]["minReplicas"] == 3
