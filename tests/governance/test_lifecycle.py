#  Project:      dfe-engine
#  File:         tests/governance/test_lifecycle.py
#  Purpose:      Service lifecycle tiers + RBAC resolution + gitops dial
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Lifecycle over a real local gitops repo (no mocks)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance.lifecycle import (
    LifecycleError,
    LifecycleState,
    ServiceTier,
    required_action,
    resolve,
    services,
    set_state,
)


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry([ResourceClass("helmvars", "values", rbac_prefix="helmvars")])
    return GitCrud(repo, registry)


def test_tiers_classify_apps_backing_and_pinned():
    assert resolve("receiver").tier == ServiceTier.MANAGED_APP
    assert resolve("hunt-runner").tier == ServiceTier.MANAGED_APP
    assert resolve("kafka").tier == ServiceTier.MANAGED_BACKING
    assert resolve("clickhouse").tier == ServiceTier.PINNED  # the store - never via API
    assert resolve("openbao").tier == ServiceTier.PINNED  # secrets manager - pinned


def test_unknown_service_raises():
    with pytest.raises(KeyError):
        resolve("not-a-service")


def test_required_action_app_vs_backing():
    assert required_action("receiver", LifecycleState.STOPPED) == "lifecycle:app:receiver"
    assert required_action("kafka", LifecycleState.STOPPED) == "lifecycle:backing:kafka"


def test_required_action_pinned_raises():
    with pytest.raises(LifecycleError):
        required_action("clickhouse", LifecycleState.STOPPED)


def test_set_state_writes_the_dial_and_commits(crud):
    res = set_state(crud, "receiver", LifecycleState.STOPPED, "bob")
    assert res.commit_sha
    assert crud.get("helmvars", "receiver")["state"] == "stopped"
    # transition again -> new commit, dial updated
    res2 = set_state(crud, "receiver", LifecycleState.PAUSED, "bob")
    assert res2.commit_sha != res.commit_sha
    assert crud.get("helmvars", "receiver")["state"] == "paused"


def test_set_state_pinned_service_is_refused(crud):
    with pytest.raises(LifecycleError):
        set_state(crud, "clickhouse", LifecycleState.PAUSED, "bob")
    # nothing written for a pinned service
    from dfe_engine.gitcrud import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        crud.get("helmvars", "clickhouse")


def test_services_lists_all_tiers():
    names = {s.name for s in services()}
    assert {"receiver", "kafka", "clickhouse"} <= names
