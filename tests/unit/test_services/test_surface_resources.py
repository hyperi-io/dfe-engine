#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_surface_resources.py
#  Purpose:      Shipped surfaces and defaults name no address, and surfaces name real options
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every shipped surface is generic, and every metric and setting it lists is its app's.

The manifests under ``tests/fixtures/contract/<app>/metrics-manifest.json`` are the
stdout of ``<app> metrics-manifest`` from a build of that app, byte for byte; each
names the commit it came from in its own ``commit`` field. Recopy one when its app
changes a metric, and this test says which surface entries no longer match.

The schemas under ``tests/fixtures/contract-main/<app>/config-schema.json`` are each
app's own ``docs/config-schema.json``, byte for byte, from the commit its
``source.json`` names. A ``config_surface`` key is ``config.`` and the option's path
in that schema, because each app's chart mounts its ``config`` block as the app's
config file. Recopy both files when an app renames, retypes or re-defaults an option.

The built-in service plugins and the configs the engine seeds ship to deployments
the engine has never seen, so none of them may name an in-cluster address either.
"""

import importlib.resources
import json
import re
from pathlib import Path

import pytest

from dfe_engine.appmgmt import contract
from dfe_engine.services.surfaces.models import ServiceSurface
from dfe_engine.yaml_utils import yaml_load
from tests.support.deployment_address import IN_CLUSTER_NAME

MANIFESTS = Path(__file__).parents[2] / "fixtures" / "contract"
SCHEMAS = Path(__file__).parents[2] / "fixtures" / "contract-main"
RESOURCES = importlib.resources.files("dfe_engine.services.surfaces.resources")
SERVICES = importlib.resources.files("dfe_engine.services")

# Any URL, or an in-cluster DNS name with the namespace in it, is one deployment's address.
_CLUSTER_ADDRESS = re.compile(rf"{IN_CLUSTER_NAME.pattern}|://")


def _shipped() -> list[str]:
    names = sorted(r.name for r in RESOURCES.iterdir() if r.name.endswith(".yaml"))
    assert names, "no built-in surfaces found"
    return names


def _raw(name: str) -> dict:
    with importlib.resources.as_file(RESOURCES / name) as path:
        return yaml_load(path)


def _surface(name: str) -> ServiceSurface:
    return ServiceSurface(**_raw(name))


def _manifest(service: str) -> dict:
    return json.loads((MANIFESTS / service / "metrics-manifest.json").read_text())


def _options(service: str) -> dict[str, contract.ConfigField]:
    """Every option the app's schema declares, keyed ``config.<path>`` as a surface keys it."""
    app = contract.load_contract(service, SCHEMAS)
    assert app.available, f"no config schema for {service} under {SCHEMAS}"
    return {option.path: option for option in contract.resolve_config(app, {}).fields}


def _service_defaults() -> list[str]:
    """Every built-in plugin module and seeded config, as ``<directory>/<file>``."""
    shipped = [
        f"{directory}/{entry.name}"
        for directory, suffix in (("plugins_builtin", ".py"), ("default_configs", ".yaml"))
        for entry in (SERVICES / directory).iterdir()
        if entry.name.endswith(suffix)
    ]
    assert shipped, "no built-in plugins or seeded configs found"
    return sorted(shipped)


@pytest.mark.parametrize("name", _shipped())
class TestShippedSurface:
    def test_names_no_address(self, name):
        text = (RESOURCES / name).read_text()

        assert not _CLUSTER_ADDRESS.search(text), f"{name} carries a deployment's address"
        assert "manifest_url" not in _raw(name)

    def test_is_the_app_its_file_names(self, name):
        assert _surface(name).service == name.removesuffix(".yaml")

    def test_has_the_app_s_manifest_beside_it(self, name):
        manifest = _manifest(_surface(name).service)

        assert manifest["app"] == _surface(name).service
        # Names below are bare only while the app runs with no metrics namespace.
        assert manifest["namespace"] == ""

    def test_every_metric_is_one_the_app_publishes(self, name):
        surface = _surface(name)
        published = {m["name"]: m for m in _manifest(surface.service)["metrics"]}

        assert surface.metrics_surface, f"{name} lists no metrics"
        for entry in surface.metrics_surface:
            assert entry.name in published, f"{surface.service} publishes no {entry.name}"
            descriptor = published[entry.name]
            assert entry.type == descriptor["type"], entry.name
            assert entry.labels == descriptor["labels"], entry.name
            assert entry.group == descriptor["group"], entry.name
            assert entry.description == descriptor["description"], entry.name

    def test_lists_each_metric_once(self, name):
        names = [entry.name for entry in _surface(name).metrics_surface]

        assert len(names) == len(set(names))

    def test_has_the_app_s_config_schema_beside_it(self, name):
        app = contract.load_contract(_surface(name).service, SCHEMAS)

        assert app.available
        assert re.fullmatch(rf"hyperi-io/{app.service}@[0-9a-f]{{40}}", app.pinned_ref or "")

    def test_every_setting_is_one_the_app_reads(self, name):
        surface = _surface(name)
        options = _options(surface.service)

        assert surface.config_surface, f"{name} lists no settings"
        for key, entry in surface.config_surface.items():
            assert key in options, f"{surface.service} reads no {key}"
            assert entry.type == options[key].type, key
            assert entry.default == options[key].default, key


@pytest.mark.parametrize("shipped", _service_defaults())
def test_no_service_default_names_an_in_cluster_address(shipped):
    directory, _, filename = shipped.partition("/")
    text = (SERVICES / directory / filename).read_text(encoding="utf-8")

    assert not IN_CLUSTER_NAME.search(text), f"{shipped} names a deployment's address"


class TestTheAddressCheck:
    """The pattern catches the shapes an address takes, so a clean pass means something."""

    @pytest.mark.parametrize(
        "line",
        [
            'manifest_url: "http://dfe-loader.dfe-prod.svc.cluster.local:9090/metrics/manifest"',
            "host: dfe-loader.acme.svc:9090",
            "url: https://metrics.example.com/manifest",
        ],
    )
    def test_an_address_is_caught(self, line):
        assert _CLUSTER_ADDRESS.search(line)

    def test_a_metric_description_is_not(self):
        assert not _CLUSTER_ADDRESS.search('description: "Records served per second"')

    @pytest.mark.parametrize(
        "line",
        [
            "    - kafka-bootstrap.kafka.svc.cluster.local:9092",
            '  address: "dfe-loader.dfe.svc:6000"',
        ],
    )
    def test_a_namespaced_service_name_is_caught(self, line):
        assert IN_CLUSTER_NAME.search(line)

    @pytest.mark.parametrize(
        "line", ['  address: "dfe-loader:6000"', '  destination: "s3://dfe-archive"']
    )
    def test_a_bare_service_name_or_a_bucket_is_not(self, line):
        assert not IN_CLUSTER_NAME.search(line)
