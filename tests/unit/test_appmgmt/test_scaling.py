#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_scaling.py
#  Purpose:      Tests for the scaling facade and its deploy-target gate
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Scaling dials: the chart key paths, the validation, and the k8s gate."""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue, scaling
from dfe_engine.appmgmt.scaling import DeployTarget, InvalidDialError

VRL = catalogue.descriptor("dfe-transform-vrl")


def test_dials_apply_on_kubernetes():
    supported, reason = scaling.support(VRL, DeployTarget.KUBERNETES)
    assert supported is True
    assert reason == ""


def test_dials_are_unsupported_on_docker_with_a_reason():
    supported, reason = scaling.support(VRL, DeployTarget.DOCKER)
    assert supported is False
    assert "docker" in reason


def test_dials_are_unsupported_when_the_target_is_unknown():
    supported, reason = scaling.support(VRL, DeployTarget.UNKNOWN)
    assert supported is False
    assert "unknown" in reason


def test_read_off_docker_reports_unsupported_rather_than_values():
    doc = {"keda": {"minReplicaCount": 2}}
    dials = scaling.read(doc, VRL, DeployTarget.DOCKER)
    assert dials.supported is False
    assert dials.min_replicas is None


def test_read_returns_the_overlay_values():
    doc = {
        "replicaCount": 3,
        "keda": {"enabled": True, "minReplicaCount": 2, "maxReplicaCount": 20},
        "resources": {
            "requests": {"cpu": "200m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },
    }
    dials = scaling.read(doc, VRL, DeployTarget.KUBERNETES)
    assert dials.replica_count == 3
    assert (dials.min_replicas, dials.max_replicas) == (2, 20)
    assert dials.keda_enabled is True
    assert (dials.cpu_request, dials.memory_request) == ("200m", "256Mi")
    assert (dials.cpu_limit, dials.memory_limit) == ("1", "512Mi")


def test_unset_dials_read_as_none_not_as_a_default():
    # An unset dial takes its value from the chart or profile layer beneath the
    # overlay, which is a different thing from being pinned to that value.
    dials = scaling.read({}, VRL, DeployTarget.KUBERNETES)
    assert dials.min_replicas is None
    assert dials.cpu_request is None


def test_changes_target_the_dfe_infra_chart_key_paths():
    out = scaling.changes({}, min_replicas=2, max_replicas=10, cpu_request="500m")
    assert out == {
        "keda.minReplicaCount": 2,
        "keda.maxReplicaCount": 10,
        "resources.requests.cpu": "500m",
    }


def test_a_partial_update_leaves_other_dials_alone():
    out = scaling.changes({"keda": {"minReplicaCount": 2}}, max_replicas=8)
    assert out == {"keda.maxReplicaCount": 8}


def test_min_below_one_is_refused():
    with pytest.raises(InvalidDialError, match="at least 1"):
        scaling.changes({}, min_replicas=0)


def test_max_below_min_is_refused():
    with pytest.raises(InvalidDialError, match="below min_replicas"):
        scaling.changes({}, min_replicas=5, max_replicas=2)


def test_raising_only_the_ceiling_validates_against_the_stored_floor():
    doc = {"keda": {"minReplicaCount": 4}}
    with pytest.raises(InvalidDialError, match="below min_replicas"):
        scaling.changes(doc, max_replicas=2)
    assert scaling.changes(doc, max_replicas=9) == {"keda.maxReplicaCount": 9}


def test_absurd_ceiling_is_refused():
    with pytest.raises(InvalidDialError, match="ceiling"):
        scaling.changes({}, max_replicas=scaling.MAX_REPLICAS_CEILING + 1)


@pytest.mark.parametrize("good", ["500m", "2", "0.5", "1000m"])
def test_valid_cpu_quantities(good):
    assert scaling.changes({}, cpu_request=good) == {"resources.requests.cpu": good}


@pytest.mark.parametrize("bad", ["2 cores", "500M", "", "-1", "m500"])
def test_invalid_cpu_quantities_are_refused(bad):
    with pytest.raises(InvalidDialError, match="cpu quantity"):
        scaling.changes({}, cpu_request=bad)


@pytest.mark.parametrize("good", ["512Mi", "2Gi", "1000000", "1.5Gi"])
def test_valid_memory_quantities(good):
    assert scaling.changes({}, memory_request=good) == {"resources.requests.memory": good}


@pytest.mark.parametrize("bad", ["512 MiB", "lots", "", "-2Gi"])
def test_invalid_memory_quantities_are_refused(bad):
    with pytest.raises(InvalidDialError, match="memory quantity"):
        scaling.changes({}, memory_request=bad)


def test_keda_toggle_is_carried_through():
    assert scaling.changes({}, keda_enabled=False) == {"keda.enabled": False}


def test_no_supplied_dials_produces_no_changes():
    assert scaling.changes({}) == {}


def test_every_catalogued_app_is_scale_deployed():
    # All seven ride the shared dfe-common.scaledobject helper, so the dial set is
    # uniform and the facade needs no per-app special casing.
    assert all(app.scale_deployed for app in catalogue.APP_CATALOGUE.values())
