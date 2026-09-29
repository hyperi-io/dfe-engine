"""A deployment the engine generates never scales on raw consumer-group lag.

The chart's own triggers -- CPU, plus the gated ScalingPressure trigger where it is
enabled -- apply only while the engine's overlay carries no ``keda.triggers`` list,
because a list replaces them outright. Raw lag also rises when a downstream stage is
broken, so a default lag trigger adds pods exactly where they cannot help.

Each test follows one path a default takes into a deploy config: the plugin
defaults, the template generator, the seed endpoint's shipped files, and the resize
endpoint's sizing defaults.
"""

import pytest

from dfe_engine.deployment.registry import DeploymentConfigRegistry
from dfe_engine.deployment.templates import generate_template
from dfe_engine.services.plugins import all_plugins, valid_services

SERVICES = sorted(valid_services())
PROFILES = ("default", "production")
TRIGGER_FIELDS = ("kafka_trigger", "cpu_trigger", "prometheus_trigger")
BOUNDS = {"min_replicas", "max_replicas", "polling_interval", "cooldown_period"}


@pytest.fixture
def deploy(tmp_path):
    registry = DeploymentConfigRegistry(config_directory=tmp_path / "deployments")
    yield registry
    registry.close()


def _named_triggers(keda: dict) -> list[str]:
    named = [field for field in TRIGGER_FIELDS if keda.get(field) is not None]
    if keda.get("extra_triggers"):
        named.append("extra_triggers")
    return named


def test_no_builtin_plugin_default_names_a_trigger():
    offenders = {
        name: sorted(set(plugin.keda_defaults) - BOUNDS)
        for name, plugin in all_plugins().items()
        if set(plugin.keda_defaults) - BOUNDS
    }

    assert offenders == {}, f"plugin KEDA defaults carry more than replica bounds: {offenders}"


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("service", SERVICES)
def test_a_generated_template_names_no_trigger(service: str, profile: str):
    keda = generate_template(service, profile=profile)["keda"]

    assert _named_triggers(keda) == [], f"{service}/{profile}: {keda}"


def test_every_seeded_deploy_config_names_no_trigger(deploy):
    assert deploy.seed_defaults() > 0

    named: dict[str, list[str]] = {}
    refused: dict[str, list[str]] = {}
    for entry in deploy.list_configs():
        service, instance = entry["service"], entry["instance"]
        key = f"{service}-{instance}"
        stored = deploy.get_config(service, instance).model_dump(mode="json")
        verdict = deploy.validate(service, stored)
        if not verdict.valid:
            refused[key] = verdict.errors
        if triggers := _named_triggers(stored["keda"]):
            named[key] = triggers

    assert named == {}, f"seeded configs name a trigger: {named}"
    assert refused == {}, f"seeded configs fail their own validation: {refused}"


@pytest.mark.parametrize("service", SERVICES)
def test_resizing_writes_no_trigger(service: str, deploy):
    deploy.apply_size(service, "sized", "small")

    keda = deploy.get_config(service, "sized").keda.model_dump(mode="json")
    assert _named_triggers(keda) == [], f"{service} resize wrote {keda}"
