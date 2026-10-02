#  Project:      dfe-engine
#  File:         tests/governance/test_policies.py
#  Purpose:      Tests for protected-var policy matching + enforcement
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Protected-var policy over a real local gitops repo."""

from importlib import resources

import pytest

from dfe_engine.gitcrud import GitCrud, ResourceClass, ResourceClassRegistry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore, ProtectedPolicy, ProtectedVarError
from dfe_engine.governance.policies import _POLICY_CLASS
from dfe_engine.yaml_utils import yaml_load_string


def _shipped(name: str) -> dict:
    """A seeded policy as the engine ships it, so a pattern added there is tested here."""
    return yaml_load_string(
        resources.files("dfe_engine.governance.resources.policies")
        .joinpath(f"{name}.yaml")
        .read_text(encoding="utf-8")
    )


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


@pytest.fixture
def shipped(crud):
    """Every policy the engine seeds, loaded through the same store the API uses."""
    for name in ("baseline", "sizing-locks", "storage-layout"):
        crud.put(_POLICY_CLASS, name, _shipped(name), actor="admin")
    return PolicyStore(crud)


class TestWritesThatReachALock:
    """A set or delete changes every var on its own line of the tree, so the lock
    has to hold at the leaf, above it and below it."""

    def test_the_locked_leaf_itself(self, shipped):
        assert shipped.matching_pattern("infravars", "kafka", "kafka.sizing.peakMbS") == (
            "infravars:*:kafka.sizing.*"
        )

    def test_the_parent_of_a_star_lock(self, shipped):
        """A map written at kafka.sizing replaces every sizing input under it."""
        assert shipped.matching_pattern("infravars", "kafka", "kafka.sizing") == (
            "infravars:*:kafka.sizing.*"
        )

    def test_the_grandparent(self, shipped):
        assert shipped.is_protected("infravars", "kafka", "kafka") is True
        assert shipped.is_protected("infravars", "common", "clickhouse") is True

    def test_the_parent_of_a_literal_lock(self, crud):
        """kafka.mode carries no star, and its parent still reaches it."""
        crud.put(_POLICY_CLASS, "storage-layout", _shipped("storage-layout"), actor="admin")
        assert PolicyStore(crud).matching_pattern("infravars", "kafka", "kafka") == (
            "infravars:*:kafka.mode"
        )

    def test_the_helmvars_image_map(self, shipped):
        assert shipped.matching_pattern("helmvars", "receiver-default", "image") == (
            "helmvars:*:image.*"
        )

    @pytest.mark.parametrize(
        ("path", "pattern"),
        [
            ("kafka.storageModel.x", "infravars:*:kafka.storageModel"),
            ("cloud.provider", "infravars:*:cloud"),
            ("kafka.controllerPool.enabled.x", "infravars:*:kafka.controllerPool.enabled"),
        ],
    )
    def test_below_a_locked_scalar(self, shipped, path, pattern):
        """A set below a scalar turns the scalar into a map, which changes it."""
        assert shipped.matching_pattern("infravars", "kafka", path) == pattern

    @pytest.mark.parametrize(
        "path",
        [
            "kafka.replicas",
            "kafka.resources.requests.cpu",
            "kafka.controllerPool.replicas",
            "clickhouse.keeper.replicas",
            # Segment boundaries: neither is on a locked line.
            "kafka.sizingNotes",
            "kafka.storage.retention",
            "cloudRegion",
        ],
    )
    def test_a_sibling_off_every_locked_line_stays_writable(self, shipped, path):
        assert shipped.matching_pattern("infravars", "kafka", path) is None

    def test_a_star_in_the_name_does_not_run_into_the_path(self, shipped):
        """infravars:*:cloud would match network.x:cloud if the star crossed the colon,
        which would lock every parent in the class."""
        assert shipped.is_protected("infravars", "kafka", "network") is False
        assert shipped.is_protected("infravars", "kafka", "kafka.objectStore") is True

    def test_the_name_scope_still_applies_above_the_lock(self, policy):
        assert policy.is_protected("helmvars", "receiver-default", "config") is True
        assert policy.is_protected("helmvars", "loader-default", "config") is False

    def test_a_bracket_set_in_the_pattern(self, crud):
        crud.put(
            _POLICY_CLASS,
            "sets",
            ProtectedPolicy(name="sets", protected=["infravars:*:kafka.[mM]ode"]).model_dump(),
            actor="admin",
        )
        store = PolicyStore(crud)
        assert store.is_protected("infravars", "kafka", "kafka") is True
        assert store.is_protected("infravars", "kafka", "kafka.Mode") is True
        assert store.is_protected("infravars", "kafka", "kafka.node") is False

    def test_a_list_parent_reaches_its_items(self, crud):
        crud.put(
            _POLICY_CLASS,
            "lists",
            ProtectedPolicy(name="lists", protected=["library:*:labels"]).model_dump(),
            actor="admin",
        )
        assert PolicyStore(crud).is_protected("library", "x", "labels[0]") is True

    def test_enforce_refuses_the_parent_without_override(self, shipped):
        with pytest.raises(ProtectedVarError) as exc:
            shipped.enforce("infravars", "kafka", "kafka.sizing")
        assert exc.value.pattern == "infravars:*:kafka.sizing.*"
        assert exc.value.path == "kafka.sizing"

    def test_enforce_passes_the_parent_with_override(self, shipped):
        shipped.enforce("infravars", "kafka", "kafka", override=True)


class TestWholeDocumentWrites:
    def test_dropping_a_locked_leaf_is_refused(self, shipped):
        before = {"kafka": {"sizing": {"peakMbS": 50}, "replicas": 3}}
        after = {"kafka": {"replicas": 3}}
        with pytest.raises(ProtectedVarError) as exc:
            shipped.enforce_document("infravars", "kafka", before, after)
        assert exc.value.path == "kafka.sizing.peakMbS"

    def test_a_scalar_over_a_locked_map_is_refused(self, shipped):
        with pytest.raises(ProtectedVarError) as exc:
            shipped.enforce_document("helmvars", "r", {}, {"image": "nginx:latest"})
        assert exc.value.path == "image"

    def test_an_unlocked_document_writes(self, shipped):
        before = {"kafka": {"replicas": 3}}
        after = {"kafka": {"replicas": 5, "resources": {"requests": {"cpu": "2"}}}}
        assert shipped.enforce_document("infravars", "kafka", before, after) is False

    def test_override_reports_the_protected_write(self, shipped):
        before = {"kafka": {"sizing": {"peakMbS": 50}}}
        assert shipped.enforce_document("infravars", "kafka", before, {}, override=True) is True
