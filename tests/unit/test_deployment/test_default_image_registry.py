"""Every image the engine defaults to is the one an outside deployment can pull.

Each test follows one path a default image takes towards a deployment: the seed
endpoint's shipped files, the deployment models a resize builds from, and the
service descriptors.
"""

from dfe_engine.deployment.registry import DeploymentConfigRegistry
from dfe_engine.services.plugins import all_plugins, deployment_classes

REGISTRY = "ghcr.io/hyperi-io"


def _published(service: str) -> str:
    return f"{REGISTRY}/dfe-{service}"


def test_every_seeded_deploy_config_names_the_published_image(tmp_path):
    deploy = DeploymentConfigRegistry(config_directory=tmp_path / "deployments")
    try:
        assert deploy.seed_defaults() > 0
        seeded = {
            (entry["service"], entry["instance"]): deploy.get_config(
                entry["service"], entry["instance"]
            ).image
            for entry in deploy.list_configs()
        }
    finally:
        deploy.close()

    assert seeded, "the seed shipped no deploy configs"
    wrong = {key: image for key, image in seeded.items() if image != _published(key[0])}
    assert wrong == {}, f"seeded configs name an image nobody outside can pull: {wrong}"


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
