#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_name_budget.py
#  Purpose:      Tests for the instance-name budget a Helm release name leaves
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A per-config app's instance name, measured against the Helm release it becomes."""

import pytest

from dfe_engine.appmgmt import catalogue, instances, scaling
from dfe_engine.appmgmt.instances import (
    ARGO_CLUSTER_NAME_ALLOWANCE,
    HELM_RELEASE_NAME_MAX,
    InstanceNameTooLongError,
)
from dfe_engine.appmgmt.scaling import DeployTarget

# Worked by hand: 53, less the service, the 19 kept for the cluster, and two dashes.
BUDGETS = {
    "dfe-fetcher": 21,
    "dfe-transform-vrl": 15,
    "dfe-transform-vector": 12,
    "dfe-transform-elastic": 11,
}

STACK_WIDE = ("dfe-receiver", "dfe-loader", "dfe-archiver", "dfe-engine", "dfe-ui")


def _budget(service: str, target: DeployTarget = DeployTarget.KUBERNETES) -> int | None:
    return scaling.instance_name_budget(catalogue.descriptor(service), target)


@pytest.mark.parametrize(("service", "budget"), BUDGETS.items())
def test_each_per_config_app_takes_what_its_release_name_leaves(service, budget):
    assert _budget(service) == budget


def test_the_budgets_cover_every_per_config_app_in_the_manifest():
    per_config = {
        name
        for name in catalogue.services()
        if catalogue.descriptor(name).multiplicity is catalogue.Multiplicity.PER_CONFIG
    }
    assert per_config == set(BUDGETS)


@pytest.mark.parametrize(("service", "budget"), BUDGETS.items())
def test_a_name_at_the_budget_fills_the_release_name_exactly(service, budget):
    # The ApplicationSet's own shape: {service}-{instance}-{cluster}.
    release = f"{service}-{'n' * budget}-{'c' * ARGO_CLUSTER_NAME_ALLOWANCE}"
    assert len(release) == HELM_RELEASE_NAME_MAX


def test_the_release_name_argo_refused_is_over_the_budget():
    # Argo refused this one at 55 characters, on a local-dfe cluster with env local.
    app = instances.instance_of("dfe-transform-vector", "umbrellavector")
    assert len(f"{app.service}-{app.instance}-dfe-local-dfe-local") == 55

    with pytest.raises(InstanceNameTooLongError, match="at most 12 for dfe-transform-vector"):
        instances.check_name_budget(app, _budget(app.service))


@pytest.mark.parametrize(("service", "budget"), BUDGETS.items())
def test_a_name_at_the_budget_passes_and_one_more_is_refused(service, budget):
    instances.check_name_budget(instances.instance_of(service, "a" * budget), budget)

    over = instances.instance_of(service, "a" * (budget + 1))
    with pytest.raises(InstanceNameTooLongError) as refused:
        instances.check_name_budget(over, budget)
    message = str(refused.value)
    assert f"is {budget + 1} characters" in message
    assert f"at most {budget} for {service}" in message


@pytest.mark.parametrize("name", ["edge", "auth", "okta-audit", "a"])
def test_short_names_pass_on_every_per_config_app(name):
    for service, budget in BUDGETS.items():
        instances.check_name_budget(instances.instance_of(service, name), budget)


@pytest.mark.parametrize("service", STACK_WIDE)
def test_a_stack_wide_app_is_not_held_to_it_on_any_target(service):
    for target in DeployTarget:
        assert _budget(service, target) is None


@pytest.mark.parametrize("service", BUDGETS)
def test_compose_renders_no_release_name_so_takes_no_budget(service):
    assert _budget(service, DeployTarget.DOCKER) is None


@pytest.mark.parametrize("service", BUDGETS)
def test_an_unknown_target_is_not_assumed_to_be_compose(service):
    assert _budget(service, DeployTarget.UNKNOWN) == BUDGETS[service]


def test_no_budget_checks_nothing():
    instances.check_name_budget(instances.instance_of("dfe-transform-elastic", "e" * 40), None)


def test_the_refusal_names_what_the_caller_calls_it():
    app = instances.instance_of("dfe-fetcher", "f" * 22)
    with pytest.raises(InstanceNameTooLongError, match=r"^source name 'f{22}' is 22 characters"):
        instances.check_name_budget(app, 21, kind="source")
