"""Every image the engine defaults to is the one an outside deployment can pull.

Each test follows one path a default image takes towards a deployment: the seed
endpoint's shipped files compiled into the overlay file the deploy repo would receive,
the deployment models a resize builds from, the service descriptors, and the
environment's registry default.
"""

import pytest

from dfe_engine.deployment.registry import DeploymentConfigRegistry
from dfe_engine.gitops.artifacts import collect_deploy_artifacts
from dfe_engine.helm.compiler import HelmValuesCompiler
from dfe_engine.helm.environment import (
    ClickHouseEnvironment,
    EnvironmentConfig,
    KafkaEnvironment,
)
from dfe_engine.helm.models import CompilationResult
from dfe_engine.services.plugins import all_plugins, deployment_classes
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.yaml_utils import yaml_load_string

REGISTRY = "ghcr.io/hyperi-io"


def _published(service: str) -> str:
    return f"{REGISTRY}/dfe-{service}"


@pytest.fixture
def environment():
    return EnvironmentConfig(
        name="test",
        kafka=KafkaEnvironment(bootstrap_servers=["kafka-1.test:9092"]),
        clickhouse=ClickHouseEnvironment(hosts=["ch-1.test:8123"]),
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


def test_every_seeded_deploy_config_renders_the_published_image(registries, environment):
    deploy, services, sources = registries
    assert deploy.seed_defaults() > 0
    compiler = HelmValuesCompiler(deploy, services, sources, environment)

    rendered: dict[tuple[str, str], str] = {}
    for entry in deploy.list_configs():
        service, instance = entry["service"], entry["instance"]
        key = f"{service}-{instance}"
        values = compiler.compile_service(service, instance)
        artifacts = collect_deploy_artifacts(CompilationResult(helm_values={key: values}))
        overlay = yaml_load_string(artifacts[f"values/{key}-values.yaml"])
        rendered[(service, instance)] = overlay["image"]["repository"]

    assert rendered, "the seed shipped no deploy configs to compile"
    wrong = {k: repo for k, repo in rendered.items() if repo != _published(k[0])}
    assert wrong == {}, f"seeded configs render an image nobody outside can pull: {wrong}"


def test_every_deployment_model_defaults_to_the_published_image():
    wrong = {
        name: cls().image
        for name, cls in deployment_classes().items()
        if cls().image != _published(name)
    }

    assert wrong == {}, f"deployment model defaults: {wrong}"


def test_every_service_descriptor_names_the_published_image():
    wrong = {
        name: plugin.descriptor.image
        for name, plugin in all_plugins().items()
        if plugin.descriptor.image != _published(name)
    }

    assert wrong == {}, f"service descriptors: {wrong}"


def test_the_environment_defaults_to_the_published_registry(environment):
    assert environment.image_registry == REGISTRY
