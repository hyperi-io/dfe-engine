#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_surface_resources.py
#  Purpose:      The built-in service surfaces hold no address and name real metrics
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every shipped surface is generic, and every metric it lists is one its app publishes.

The manifests under ``tests/fixtures/contract/<app>/metrics-manifest.json`` are the
stdout of ``<app> metrics-manifest`` from a build of that app, byte for byte; each
names the commit it came from in its own ``commit`` field. Recopy one when its app
changes a metric, and this test says which surface entries no longer match.
"""

import importlib.resources
import json
import re
from pathlib import Path

import pytest

from dfe_engine.services.surfaces.models import ServiceSurface
from dfe_engine.yaml_utils import yaml_load

MANIFESTS = Path(__file__).parents[2] / "fixtures" / "contract"
RESOURCES = importlib.resources.files("dfe_engine.services.surfaces.resources")

# Any URL, or an in-cluster DNS name with the namespace in it, is one deployment's address.
_CLUSTER_ADDRESS = re.compile(r"\.svc(\.cluster\.local)?\b|://")


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
