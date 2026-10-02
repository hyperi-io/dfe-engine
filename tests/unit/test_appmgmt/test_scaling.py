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


LOADER = catalogue.descriptor("dfe-loader")


def test_a_per_config_app_is_unbounded_on_kubernetes():
    assert scaling.instance_ceiling(VRL, DeployTarget.KUBERNETES) is None


def test_a_per_config_app_cannot_be_run_on_docker_without_a_writer():
    # Compose holds one service per app, and where nothing renders a config into
    # it an instance written here would never run.
    assert scaling.instance_ceiling(VRL, DeployTarget.DOCKER) == 0


def test_a_docker_target_that_renders_its_app_config_is_unbounded():
    # Each instance gets its own rendered directory and its name reaches the
    # deployer's instance index, so Compose declares a container per source.
    assert scaling.instance_ceiling(VRL, DeployTarget.DOCKER, writes_app_config=True) is None


def test_a_single_deployment_app_is_one_everywhere():
    for target in DeployTarget:
        assert scaling.instance_ceiling(LOADER, target) == 1


def test_an_unknown_target_is_not_assumed_to_be_compose():
    assert scaling.instance_ceiling(VRL, DeployTarget.UNKNOWN) is None


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


class TestReplicaCount:
    """The KEDA-off dial: without it a deployment with KEDA disabled has no count."""

    def test_a_fixed_count_is_written_when_keda_is_off(self):
        doc = {"keda": {"enabled": False}}
        assert scaling.changes(doc, replica_count=3) == {"replicaCount": 3}

    def test_an_unset_keda_flag_refuses_a_count(self):
        # A scale-deployed app's chart runs KEDA unless the overlay turns it off, and
        # the commit policy refuses the stored count on the same reading.
        with pytest.raises(InvalidDialError, match="KEDA is enabled by the chart default"):
            scaling.changes({}, replica_count=2)

    def test_an_unset_keda_flag_accepts_a_count_where_the_chart_runs_no_keda(self):
        assert scaling.changes({}, replica_count=2, keda_by_default=False) == {"replicaCount": 2}

    def test_turning_keda_off_in_the_same_request_accepts_a_count(self):
        doc = {"keda": {"enabled": True}}
        assert scaling.changes(doc, replica_count=4, keda_enabled=False) == {
            "keda.enabled": False,
            "replicaCount": 4,
        }

    def test_a_count_is_refused_while_keda_is_explicitly_on(self):
        doc = {"keda": {"enabled": True}}
        with pytest.raises(InvalidDialError, match="KEDA is enabled"):
            scaling.changes(doc, replica_count=3)

    def test_zero_is_allowed_as_a_deliberate_stop(self):
        assert scaling.changes({"keda": {"enabled": False}}, replica_count=0) == {"replicaCount": 0}

    def test_a_negative_count_is_refused(self):
        with pytest.raises(InvalidDialError, match="negative"):
            scaling.changes({}, replica_count=-1)

    def test_an_absurd_count_is_refused(self):
        with pytest.raises(InvalidDialError, match="ceiling"):
            scaling.changes({}, replica_count=scaling.MAX_REPLICAS_CEILING + 1)

    def test_the_written_path_is_the_one_read_reports(self):
        changed = scaling.changes({"keda": {"enabled": False}}, replica_count=6)
        doc: dict = {}
        for path, value in changed.items():
            doc.setdefault(path, value)
        assert scaling.read(doc, VRL, DeployTarget.KUBERNETES).replica_count == 6


def test_no_supplied_dials_produces_no_changes():
    assert scaling.changes({}) == {}


def test_scale_deployed_apps_ride_one_uniform_dial_set():
    # Every scale-deployed app uses the shared dfe-common.scaledobject helper, so
    # the facade needs no per-app special casing.
    scaled = [a for a in catalogue.APP_CATALOGUE.values() if a.scale_deployed]
    assert scaled
    assert all(scaling.support(app, DeployTarget.KUBERNETES)[0] for app in scaled)


def test_an_app_that_does_not_scale_reports_the_dials_unsupported():
    # dfe-fetcher polls its upstream rather than draining a queue, so it carries no
    # KEDA dials and the surface must say so rather than offering dials that do
    # nothing.
    fetcher = catalogue.descriptor("dfe-fetcher")
    assert fetcher.scale_deployed is False
    supported, reason = scaling.support(fetcher, DeployTarget.KUBERNETES)
    assert supported is False
    assert "dfe-fetcher" in reason


class TestKedaByDefault:
    """Which deploy-repo documents the commit policy's KEDA rule reads as KEDA-driven."""

    def test_an_overlay_of_a_scale_deployed_app_runs_keda(self):
        assert scaling.keda_by_default("helmvars", "dfe-receiver-default-values") is True

    def test_an_overlay_of_an_app_that_does_not_scale_runs_none(self):
        # dfe-fetcher's manifest entry is scale_deployed: false, and its chart ships KEDA off.
        assert catalogue.descriptor("dfe-fetcher").scale_deployed is False
        assert scaling.keda_by_default("helmvars", "dfe-fetcher-poller-values") is False

    @pytest.mark.parametrize("name", ["receiver-default", "not-an-app-values", "dfe-receiver"])
    def test_an_overlay_the_catalogue_cannot_place_keeps_the_strict_reading(self, name):
        assert scaling.keda_by_default("helmvars", name) is True

    @pytest.mark.parametrize("name", ["hyperdx", "ferretdb", "clickhouse-cluster", "common"])
    def test_a_platform_chart_runs_none(self, name):
        assert scaling.keda_by_default("infravars", name) is False

    def test_every_scale_deployed_app_reads_as_keda_driven(self):
        for service, app in catalogue.APP_CATALOGUE.items():
            name = f"{service}-default-values"
            assert scaling.keda_by_default("helmvars", name) is app.scale_deployed, service


def test_multiplicity_is_independent_of_scaling():
    # A transform is both per-config and KEDA-scaled: there may be hundreds, one
    # per source, each replicating on its own load.
    vrl = catalogue.descriptor("dfe-transform-vrl")
    assert vrl.multiplicity is catalogue.Multiplicity.PER_CONFIG
    assert vrl.scale_deployed is True
