#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_instances.py
#  Purpose:      Tests for app-instance identity and lifecycle
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Instance identity, overlay naming and lifecycle over a real git repo."""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue, instances
from dfe_engine.appmgmt.instances import (
    HELMVARS_CLASS,
    AppInstance,
    InvalidInstanceError,
)

VRL = "dfe-transform-vrl"


def test_overlay_name_matches_the_deploy_repo_convention():
    app = instances.instance_of(VRL, "edge")
    assert app.overlay_name == "dfe-transform-vrl-edge-values"


def test_telemetry_name_distinguishes_instances_of_one_app():
    # scalo defaults OTel service.name to the app name, so two instances would be
    # indistinguishable in the otel database without this.
    first = instances.instance_of(VRL, "edge")
    second = instances.instance_of(VRL, "core")
    assert first.telemetry_name != second.telemetry_name


@pytest.mark.parametrize(
    "bad",
    ["", "Edge", "-edge", "edge-", "edge_1", "edge/other", "e" * 41, "..", "a.b"],
)
def test_invalid_instance_names_are_refused(bad):
    with pytest.raises(InvalidInstanceError):
        instances.validate_instance(bad)


@pytest.mark.parametrize("good", ["edge", "e", "edge-1", "a-b-c", "e" * 40])
def test_valid_instance_names_are_accepted(good):
    instances.validate_instance(good)


def test_unknown_service_is_refused():
    with pytest.raises(catalogue.UnknownAppError):
        instances.instance_of("dfe-nonesuch", "edge")


def test_parse_overlay_name_round_trips():
    app = instances.instance_of(VRL, "edge")
    assert instances.parse_overlay_name(app.overlay_name) == app


def test_parse_prefers_the_longest_matching_service():
    # dfe-transform-vrl must win over any shorter catalogued service that is a
    # prefix of it, or the instance name absorbs the rest of the service name.
    parsed = instances.parse_overlay_name("dfe-transform-vrl-edge-values")
    assert parsed == AppInstance(service=VRL, instance="edge")


@pytest.mark.parametrize(
    "name",
    ["something-else-values", "dfe-transform-vrl-values", "dfe-transform-vrl-edge", "values"],
)
def test_parse_ignores_files_that_are_not_managed_instances(name):
    assert instances.parse_overlay_name(name) is None


def test_initial_overlay_carries_what_the_appset_needs(crud):
    app = instances.instance_of(VRL, "edge")
    doc = instances.initial_overlay(app)
    # Without the deploy block the ApplicationSet produces no Application at all.
    assert doc["deploy"] == {"service": VRL, "instance": "edge"}
    # The chart's own dial. `env` is a string there (the deployment environment)
    # feeding labels and the namespace, so a map would render an invalid label.
    assert doc["otelServiceName"] == "dfe-transform-vrl-edge"


def test_initial_overlay_applies_caller_values_as_dot_paths():
    app = instances.instance_of(VRL, "edge")
    doc = instances.initial_overlay(app, {"keda.maxReplicaCount": 8})
    assert doc["keda"]["maxReplicaCount"] == 8


def test_create_then_list_then_delete(crud):
    app = instances.instance_of(VRL, "edge")
    assert instances.exists(crud, app) is False
    assert instances.list_instances(crud) == []

    crud.put(HELMVARS_CLASS, app.overlay_name, instances.initial_overlay(app), actor="alice")

    assert instances.exists(crud, app) is True
    assert instances.list_instances(crud) == [app]
    assert instances.read_overlay(crud, app)["deploy"]["instance"] == "edge"

    crud.delete(HELMVARS_CLASS, app.overlay_name, actor="alice")
    assert instances.exists(crud, app) is False
    assert instances.list_instances(crud) == []


def test_list_filters_by_service(crud):
    vrl = instances.instance_of(VRL, "edge")
    loader = instances.instance_of("dfe-loader", "default")
    for app in (vrl, loader):
        crud.put(HELMVARS_CLASS, app.overlay_name, instances.initial_overlay(app), actor="alice")

    assert instances.list_instances(crud, service=VRL) == [vrl]
    assert instances.list_instances(crud, service="dfe-loader") == [loader]
    assert len(instances.list_instances(crud)) == 2


def test_instances_are_found_by_the_compiler_that_routes_them(crud):
    # A caller needing a particular stage asks the manifest which app runs it, so
    # renaming or replacing the loading stage does not need an engine release.
    from dfe_engine.appmgmt import LOADER_COMPILER

    vrl = instances.instance_of(VRL, "edge")
    loader = instances.instance_of("dfe-loader", "default")
    for app in (vrl, loader):
        crud.put(HELMVARS_CLASS, app.overlay_name, instances.initial_overlay(app), actor="alice")

    found = instances.instances_routed_by(crud, LOADER_COMPILER)

    assert [i.telemetry_name for i in found] == ["dfe-loader-default"]


def test_unmanaged_values_file_is_not_listed_as_an_instance(crud):
    # A hand-written overlay for something outside the catalogue stays visible to
    # the raw helm API but must not masquerade as a managed instance.
    crud.put(HELMVARS_CLASS, "some-other-thing-values", {"a": 1}, actor="alice")
    assert instances.list_instances(crud) == []
