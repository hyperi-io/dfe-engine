#  Project:      dfe-engine
#  File:         tests/governance/test_policies.py
#  Purpose:      Tests for protected-var policy matching + enforcement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Protected-var policy over a real local gitops repo."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore, ProtectedPolicy, ProtectedVarError
from dfe_engine.governance.policies import _POLICY_CLASS


@pytest.fixture
def crud(tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    registry = ResourceClassRegistry(
        [ResourceClass("policies", "governance/policies", rbac_prefix="governance")]
    )
    return GitCrud(repo, registry)


@pytest.fixture
def policy(crud):
    crud.put(
        _POLICY_CLASS,
        "lockdown",
        ProtectedPolicy(
            name="lockdown",
            protected=["helmvars:*:image.tag", "helmvars:receiver-default:config.kafka.*"],
        ).model_dump(),
        actor="admin",
    )
    return PolicyStore(crud)


def test_wildcard_name_match(policy):
    assert policy.is_protected("helmvars", "anything", "image.tag") is True


def test_scoped_path_glob_match(policy):
    assert policy.is_protected("helmvars", "receiver-default", "config.kafka.brokers") is True
    assert policy.is_protected("helmvars", "loader-default", "config.kafka.brokers") is False


def test_unprotected_var(policy):
    assert policy.is_protected("helmvars", "receiver-default", "replicaCount") is False


def test_enforce_raises_when_protected(policy):
    with pytest.raises(ProtectedVarError):
        policy.enforce("helmvars", "receiver-default", "image.tag")


def test_enforce_passes_with_override(policy):
    policy.enforce("helmvars", "receiver-default", "image.tag", override=True)  # no raise


def test_no_policies_means_nothing_protected(crud):
    assert PolicyStore(crud).is_protected("helmvars", "x", "y") is False
