#  Project:      dfe-engine
#  File:         tests/integration/test_overlay_renders.py
#  Purpose:      The overlay-to-chart contract - engine dials against the real charts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render the engine's own overlays through the real dfe-infra charts.

The engine writes a values overlay and the chart turns it into Kubernetes objects,
but nothing above this file ever runs both halves together: a unit test proves the
engine wrote the key it meant to, and a chart test proves the chart reads the key it
expects, and neither notices when the two names stop agreeing. Every assertion here
is about that seam.

The charts come from a dfe-infra checkout (``$DFE_INFRA_DIR``, or one beside this
repo), else from the dfe-infra release pinned in ``tests/support/producer_contract.py``.
The module skips when the ``helm`` binary is absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from dfe_engine.appmgmt import catalogue, files, instances, scaling
from dfe_engine.gitcrud.engine import set_path
from dfe_engine.yaml_utils import yaml_dump_string
from tests.support.producer_contract import DFE_INFRA, producer_tree

if shutil.which("helm") is None:
    pytest.skip("the helm binary is not on PATH", allow_module_level=True)

pytestmark = pytest.mark.integration

VRL = "dfe-transform-vrl"
VECTOR = "dfe-transform-vector"
FETCHER = "dfe-fetcher"

TRANSFORM_SERVICES = [VRL, VECTOR]

# The app's own setting naming the directory it reads its transforms from. The chart
# exports it from transformFilesDir, so it outranks anything in the config blob.
TRANSFORMS_DIR_ENV = "DFE_TRANSFORM_TRANSFORMS_DIR"

# A Kubernetes label value: 63 characters or fewer, alphanumeric at both ends, and
# only alphanumerics, '-', '_' and '.' between. Empty is legal.
_LABEL_VALUE_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")

FILE_FOR = {VRL: "010_enrich.vrl", VECTOR: "010_enrich.yaml"}
BODY_FOR = {
    VRL: ".source = string!(.host)\n.ts = to_timestamp!(.timestamp)\n",
    VECTOR: "type: remap\nsource: |\n  .a = 1\n",
}

# Content shapes a naive block-scalar emitter mangles or lets escape its own scalar.
HOSTILE_BODIES = {
    "indented-first-line": "  .j = 7\n.k = 8\n",
    "leading-tab": "\t.a = 1\n.b = 2\n",
    "crlf-line-endings": ".a = 1\r\n.b = 2\r\n",
    "trailing-whitespace": ".a = 1   \n.b = 2\n",
    "empty": "",
    "document-marker": "---\n.a = 1\n",
    "carriage-return-injection": ".a = 1\rreplicaCount: 99\r",
}


@pytest.fixture(scope="module")
def charts(tmp_path_factory) -> Path:
    """dfe-infra's chart directory, from a checkout or the pinned release."""
    dest = tmp_path_factory.mktemp("dfe-infra")
    tree = producer_tree(DFE_INFRA, "helm/charts", dest)
    yield tree
    # Module-scoped tmp_path_factory dirs are NOT covered by tmp_path_retention_policy.
    shutil.rmtree(dest, ignore_errors=True)


# ── helpers ───────────────────────────────────────────────────


def label_value_ok(value: Any) -> bool:
    """Whether ``value`` is a legal Kubernetes label value."""
    if not isinstance(value, str):
        return False
    return value == "" or (len(value) <= 63 and bool(_LABEL_VALUE_RE.fullmatch(value)))


def bad_labels(doc: Any, path: str = "") -> list[str]:
    """Every label in the object whose value Kubernetes would reject."""
    found: list[str] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            here = f"{path}.{key}" if path else str(key)
            if key in {"labels", "matchLabels"} and isinstance(value, dict):
                found += [f"{here}.{k}={v!r}" for k, v in value.items() if not label_value_ok(v)]
            else:
                found += bad_labels(value, here)
    elif isinstance(doc, list):
        for i, item in enumerate(doc):
            found += bad_labels(item, f"{path}[{i}]")
    return found


def render(charts: Path, service: str, doc: dict, release: str, tmp_path: Path) -> list[dict]:
    """Write the overlay to a values file and render the real chart through helm."""
    values_file = tmp_path / f"{release}-values.yaml"
    values_file.write_text(yaml_dump_string(doc), encoding="utf-8")
    result = subprocess.run(
        ["helm", "template", release, str(charts / service), "--values", str(values_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]


def overlay_with_files(service: str, instance: str, bodies: dict[str, str]) -> dict:
    """An instance overlay carrying the given files, built by the engine's own code."""
    app = instances.instance_of(service, instance)
    doc = instances.initial_overlay(app)
    file_set = catalogue.file_set(service, "transforms")
    for name, content in bodies.items():
        files.upsert_file(doc, file_set, name, content)
    return doc


def only(objects: list[dict], kind: str) -> dict:
    """The single rendered object of that kind."""
    matching = [o for o in objects if o.get("kind") == kind]
    assert len(matching) == 1, (
        f"expected one {kind}, got {[o['metadata']['name'] for o in matching]}"
    )
    return matching[0]


def transforms_configmap(objects: list[dict]) -> dict:
    """The ConfigMap the transforms volume is sourced from."""
    volume = next(
        v
        for v in only(objects, "Deployment")["spec"]["template"]["spec"]["volumes"]
        if v["name"] == "transforms"
    )
    name = volume["configMap"]["name"]
    return next(o for o in objects if o["kind"] == "ConfigMap" and o["metadata"]["name"] == name)


def container(objects: list[dict]) -> dict:
    """The single application container in the rendered Deployment."""
    containers = only(objects, "Deployment")["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    return containers[0]


def env_of(objects: list[dict]) -> dict[str, str]:
    """The container's literal-valued environment, by name."""
    return {e["name"]: e["value"] for e in container(objects)["env"] if "value" in e}


def mount_path(objects: list[dict], volume_name: str) -> str:
    """Where the container mounts the named volume."""
    mount = next(m for m in container(objects)["volumeMounts"] if m["name"] == volume_name)
    return mount["mountPath"]


# ── the contract ──────────────────────────────────────────────


@pytest.mark.parametrize("service", TRANSFORM_SERVICES)
class TestOverlayRendersThroughTheChart:
    def test_the_chart_renders_at_all(self, charts, service, tmp_path):
        # The engine writing a key the chart does not expect is not caught by any
        # unit test on either side; a rendering failure here is the only signal.
        doc = overlay_with_files(service, "edge", {})
        objects = render(charts, service, doc, "edge", tmp_path)
        kinds = {o["kind"] for o in objects}
        assert {"Deployment", "ConfigMap", "ServiceAccount"} <= kinds

    def test_every_rendered_label_value_is_legal(self, charts, service, tmp_path):
        # Writing the OTel name under `env` turned that string into a map and the
        # chart rendered `map[OTEL_SERVICE_NAME:...]` into dfe.hyperi.io/env, which
        # the API server rejects for every object in the release.
        doc = overlay_with_files(service, "edge", {FILE_FOR[service]: BODY_FOR[service]})
        objects = render(charts, service, doc, "edge", tmp_path)
        offending = [problem for o in objects for problem in bad_labels(o)]
        assert offending == []

    def test_a_map_where_the_chart_wants_a_string_is_caught(self, charts, service, tmp_path):
        # Proves the label check has teeth: the shape of the original defect must
        # still fail it, or the assertion above passes for the wrong reason.
        doc = overlay_with_files(service, "edge", {})
        doc["env"] = {"OTEL_SERVICE_NAME": "dfe-edge"}
        objects = render(charts, service, doc, "edge", tmp_path)
        assert [problem for o in objects for problem in bad_labels(o)] != []

    def test_the_otel_service_name_reaches_the_container(self, charts, service, tmp_path):
        # The engine's dial and the chart's dial have to be the same key, and the
        # value has to arrive as the instance's telemetry name, not the app's.
        app = instances.instance_of(service, "edge")
        doc = instances.initial_overlay(app)
        objects = render(charts, service, doc, app.telemetry_name, tmp_path)
        assert env_of(objects)["OTEL_SERVICE_NAME"] == app.telemetry_name
        assert env_of(objects)["OTEL_SERVICE_NAME"] == f"{service}-edge"

    def test_two_instances_are_distinguishable_in_telemetry(self, charts, service, tmp_path):
        names = set()
        for instance in ("edge", "core"):
            app = instances.instance_of(service, instance)
            doc = instances.initial_overlay(app)
            objects = render(charts, service, doc, app.telemetry_name, tmp_path)
            names.add(env_of(objects)["OTEL_SERVICE_NAME"])
        assert len(names) == 2

    def test_a_written_file_reaches_the_configmap_and_the_mount(self, charts, service, tmp_path):
        # The whole storage decision rests on this: content lives in the overlay
        # because Helm cannot read a raw file out of an Argo $values source.
        name, body = FILE_FOR[service], BODY_FOR[service]
        doc = overlay_with_files(service, "edge", {name: body})
        objects = render(charts, service, doc, "edge", tmp_path)

        assert transforms_configmap(objects)["data"][name] == body
        # The app reads the directory this env var names, so the mount has to land
        # exactly there or the files are delivered somewhere nothing looks.
        assert mount_path(objects, "transforms") == env_of(objects)[TRANSFORMS_DIR_ENV]

    def test_files_absent_from_the_overlay_render_no_configmap(self, charts, service, tmp_path):
        doc = overlay_with_files(service, "edge", {})
        objects = render(charts, service, doc, "edge", tmp_path)
        names = {o["metadata"]["name"] for o in objects if o["kind"] == "ConfigMap"}
        assert not any(n.endswith("-transforms") for n in names)

    @pytest.mark.parametrize("case", sorted(HOSTILE_BODIES))
    def test_hostile_content_survives_byte_exact(self, charts, service, case, tmp_path):
        body = HOSTILE_BODIES[case]
        name = FILE_FOR[service]
        doc = overlay_with_files(service, "edge", {name: body})
        objects = render(charts, service, doc, "edge", tmp_path)
        assert transforms_configmap(objects)["data"][name] == body

    def test_content_cannot_forge_a_sibling_key_in_the_rendered_objects(
        self, charts, service, tmp_path
    ):
        # A body that breaks out of its own scalar would set replicaCount. The chart
        # renders replicas only while KEDA is off, so the replica count is the evidence.
        doc = overlay_with_files(
            service, "edge", {FILE_FOR[service]: HOSTILE_BODIES["carriage-return-injection"]}
        )
        for path, value in scaling.changes(doc, keda_enabled=False).items():
            set_path(doc, path, value)
        objects = render(charts, service, doc, "edge", tmp_path)
        assert only(objects, "Deployment")["spec"]["replicas"] == 1

    def test_scaling_dials_reach_the_scaledobject_and_the_container(
        self, charts, service, tmp_path
    ):
        doc = overlay_with_files(service, "edge", {})
        dials = scaling.changes(
            doc,
            min_replicas=3,
            max_replicas=17,
            cpu_request="250m",
            cpu_limit="2",
            memory_request="512Mi",
            memory_limit="2Gi",
        )
        for path, value in dials.items():
            set_path(doc, path, value)
        objects = render(charts, service, doc, "edge", tmp_path)

        scaled = only(objects, "ScaledObject")
        assert scaled["spec"]["minReplicaCount"] == 3
        assert scaled["spec"]["maxReplicaCount"] == 17

        resources = container(objects)["resources"]
        assert resources["requests"]["cpu"] == "250m"
        assert resources["requests"]["memory"] == "512Mi"
        assert resources["limits"]["cpu"] == "2"
        assert resources["limits"]["memory"] == "2Gi"

    def test_the_scaledobject_targets_the_rendered_deployment(self, charts, service, tmp_path):
        doc = overlay_with_files(service, "edge", {})
        objects = render(charts, service, doc, "edge", tmp_path)
        target = only(objects, "ScaledObject")["spec"]["scaleTargetRef"]["name"]
        assert target == only(objects, "Deployment")["metadata"]["name"]


class TestPerConfigInstancesDoNotCollide:
    """dfe-fetcher runs many configs side by side, so their object names must differ."""

    def _names(self, charts: Path, instance: str, tmp_path: Path) -> set[str]:
        app = instances.instance_of(FETCHER, instance)
        doc = instances.initial_overlay(app)
        objects = render(charts, FETCHER, doc, app.telemetry_name, tmp_path)
        assert objects
        return {f"{o['kind']}/{o['metadata']['name']}" for o in objects}

    def test_two_instances_render_no_shared_object_name(self, charts, tmp_path):
        # dfe-common.fullname is {project}-{component} with no instance of its own,
        # so without the per-instance component the two Argo Applications would own
        # the same objects and fight over them under self-heal.
        alpha = self._names(charts, "alpha", tmp_path)
        beta = self._names(charts, "beta", tmp_path)
        assert alpha & beta == set()
        assert {n.split("/", 1)[0] for n in alpha} == {n.split("/", 1)[0] for n in beta}

    def test_every_object_name_carries_the_instance(self, charts, tmp_path):
        for instance in ("alpha", "beta"):
            for name in self._names(charts, instance, tmp_path):
                assert instance in name

    def test_the_rendered_labels_stay_legal(self, charts, tmp_path):
        app = instances.instance_of(FETCHER, "alpha")
        doc = instances.initial_overlay(app)
        objects = render(charts, FETCHER, doc, app.telemetry_name, tmp_path)
        assert [problem for o in objects for problem in bad_labels(o)] == []
