"""A deployment the engine generates never scales on raw consumer-group lag.

The chart's own triggers -- CPU, plus the gated ScalingPressure trigger where it is
enabled -- apply only while the engine's overlay carries no ``keda.triggers`` list,
because a list replaces them outright. Raw lag also rises when a downstream stage is
broken, so a default lag trigger adds pods exactly where they cannot help.

Each test follows one path a default takes into a deploy config, and the compile
tests read the result back out of the overlay file the deploy repo would receive:
the template generator, the seed endpoint's shipped files, and the resize
endpoint's sizing defaults.
"""

import pytest

from dfe_engine.deployment.registry import DeploymentConfigRegistry
from dfe_engine.deployment.templates import generate_template
from dfe_engine.gitops.artifacts import collect_deploy_artifacts
from dfe_engine.helm.compiler import HelmValuesCompiler
from dfe_engine.helm.environment import (
    ClickHouseEnvironment,
    EnvironmentConfig,
    KafkaEnvironment,
)
from dfe_engine.helm.models import CompilationResult, HelmKeda
from dfe_engine.services.plugins import all_plugins, valid_services
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.yaml_utils import yaml_load_string

SERVICES = sorted(valid_services())
PROFILES = ("default", "production")
TRIGGER_FIELDS = ("kafka_trigger", "cpu_trigger", "prometheus_trigger")
BOUNDS = {"min_replicas", "max_replicas", "polling_interval", "cooldown_period"}


@pytest.fixture
def environment():
    return EnvironmentConfig(
        name="test",
        kafka=KafkaEnvironment(
            bootstrap_servers=["kafka-1.test:9092"],
            authentication_ref="kafka-auth",
        ),
        clickhouse=ClickHouseEnvironment(
            hosts=["ch-1.test:8123"],
            database="dfe",
            username="default",
        ),
    )


@pytest.fixture
def registries(tmp_path):
    deploy = DeploymentConfigRegistry(config_directory=tmp_path / "deployments")
    services = ServiceConfigRegistry(config_directory=tmp_path / "services")
    sources = SourceRegistry(sources_directory=tmp_path / "sources")
    yield deploy, services, sources
    deploy.close()
    services.close()
    sources.close()


def _compile(registries, environment, service: str, instance: str) -> tuple[HelmKeda, dict]:
    """Compile one instance; return its KEDA model and the ``keda`` block of its overlay file."""
    deploy, services, sources = registries
    values = HelmValuesCompiler(deploy, services, sources, environment).compile_service(
        service, instance
    )
    key = f"{service}-{instance}"
    artifacts = collect_deploy_artifacts(CompilationResult(helm_values={key: values}))
    return values.keda, yaml_load_string(artifacts[f"values/{key}-values.yaml"])["keda"]


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


@pytest.mark.parametrize("service", SERVICES)
def test_a_compiled_production_template_leaves_the_triggers_to_the_chart(
    service: str, registries, environment
):
    template = generate_template(service, profile="production")
    registries[0].save_config(service, template, instance="production")

    compiled, overlay = _compile(registries, environment, service, "production")

    assert compiled.triggers is None, f"{service} compiled {compiled.triggers}"
    assert "triggers" not in overlay, f"{service} overlay replaces the chart triggers: {overlay}"
    assert overlay["enabled"] is True
    assert overlay["minReplicaCount"] == template["keda"]["min_replicas"]
    assert overlay["maxReplicaCount"] == template["keda"]["max_replicas"]
    assert overlay["pollingInterval"] == template["keda"]["polling_interval"]
    assert overlay["cooldownPeriod"] == template["keda"]["cooldown_period"]


def test_an_explicit_kafka_trigger_still_compiles_to_a_kafka_trigger(registries, environment):
    template = generate_template("loader", profile="production")
    template["keda"]["kafka_trigger"] = {
        "consumer_group": "clickhouse-loader",
        "lag_threshold": 50000,
    }
    registries[0].save_config("loader", template, instance="lag-scaled")

    _, overlay = _compile(registries, environment, "loader", "lag-scaled")

    assert [trigger["type"] for trigger in overlay["triggers"]] == ["kafka"]
    metadata = overlay["triggers"][0]["metadata"]
    assert metadata["consumerGroup"] == "clickhouse-loader"
    assert metadata["lagThreshold"] == "50000"
    assert metadata["bootstrapServers"] == "kafka-1.test:9092"
    assert overlay["triggers"][0]["authenticationRef"] == {"name": "kafka-auth"}


def test_every_seeded_deploy_config_leaves_the_triggers_to_the_chart(registries, environment):
    deploy = registries[0]
    assert deploy.seed_defaults() > 0

    replaced: dict[str, list] = {}
    refused: dict[str, list[str]] = {}
    for entry in deploy.list_configs():
        service, instance = entry["service"], entry["instance"]
        key = f"{service}-{instance}"
        stored = deploy.get_config(service, instance).model_dump(mode="json")
        verdict = deploy.validate(service, stored)
        if not verdict.valid:
            refused[key] = verdict.errors
        _, overlay = _compile(registries, environment, service, instance)
        if "triggers" in overlay:
            replaced[key] = overlay["triggers"]

    assert replaced == {}, f"seeded configs replace the chart triggers: {replaced}"
    assert refused == {}, f"seeded configs fail their own validation: {refused}"


@pytest.mark.parametrize("service", SERVICES)
def test_resizing_writes_no_trigger(service: str, registries):
    deploy = registries[0]

    deploy.apply_size(service, "sized", "small")

    keda = deploy.get_config(service, "sized").keda.model_dump(mode="json")
    assert _named_triggers(keda) == [], f"{service} resize wrote {keda}"
