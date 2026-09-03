"""The chart's dials must reach a template, and the API pod must get the CH env.

Two failures this pins, both found by rendering the chart rather than reading it:

* a ``huntRunner.keda`` block with seven dials and no ``ScaledObject`` template
  to read them -- and its ``enabled: true`` default suppressed ``replicas`` on
  the hunt-runner Deployment, so ``huntRunner.replicaCount`` did nothing either;
* ``config.clickhouse.data_database`` reaching the hunt-runner and the
  materialise Job but not the API pod, which reads ``effective_data_database``
  on nearly every route.

Pure text/YAML, no ``helm`` binary and no ``rg`` -- both are absent from CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "chart"
TEMPLATES = CHART / "templates"


def _template_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(TEMPLATES.iterdir()))


def _leaf_paths(node, prefix: str = ""):
    """Yield dotted paths to every scalar (and every empty dict) in values.yaml."""
    if isinstance(node, dict):
        if not node:
            yield prefix
            return
        for key, value in node.items():
            yield from _leaf_paths(value, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix


def _is_referenced(path: str, text: str) -> bool:
    if re.search(r"\.Values\." + re.escape(path) + r"(?![\w.])", text):
        return True
    # an ancestor consumed wholesale by `with` or `toYaml` covers its children
    parts = path.split(".")
    for i in range(len(parts) - 1):
        ancestor = ".".join(parts[: i + 1])
        pattern = r"(with|toYaml)\s+\$?\.Values\." + re.escape(ancestor) + r"(?![\w.])"
        if re.search(pattern, text):
            return True
    return False


class TestEveryChartValueIsRead:
    def test_no_dial_is_a_dead_end(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
        text = _template_text()
        dead = [p for p in _leaf_paths(values) if not _is_referenced(p, text)]
        assert dead == [], (
            f"values.yaml exposes dials no template reads, so setting them does nothing: {dead}"
        )

    def test_the_check_can_see_a_value(self):
        """The scan is only worth having if an unread dial actually trips it."""
        assert not _is_referenced("huntRunner.keda.mysqlPort", _template_text())
        assert _is_referenced("huntRunner.replicaCount", _template_text())


class TestHuntRunnerReplicas:
    def test_replica_count_is_unconditional(self):
        """No ScaledObject here, so a guarded `replicas` hands the count to nothing.

        A conditional emitted nothing on the default values, and k8s then pinned
        the Deployment at one pod whatever ``replicaCount`` said.
        """
        lines = (TEMPLATES / "hunt-runner-deployment.yaml").read_text(encoding="utf-8").splitlines()
        idx = next(
            i
            for i, line in enumerate(lines)
            if line.strip() == "replicas: {{ .Values.huntRunner.replicaCount }}"
        )
        assert lines[idx - 1].strip() == "spec:", (
            f"replicas is behind a conditional: {lines[idx - 1].strip()!r} precedes it"
        )


class TestClickHouseEnvReachesEveryPod:
    """Every pod that talks to ClickHouse shares one env block."""

    @pytest.mark.parametrize(
        "template",
        [
            "deployment.yaml",
            "hunt-runner-deployment.yaml",
            "hunt-runner-materialise-job.yaml",
        ],
    )
    def test_pod_uses_the_shared_clickhouse_env(self, template: str):
        text = (TEMPLATES / template).read_text(encoding="utf-8")
        assert 'include "dfe-engine.clickhouseEnv"' in text
        assert "DFE_CLICKHOUSE_HOST" not in text, (
            f"{template} inlines the ClickHouse env instead of using the shared "
            "include; an inlined copy is how data_database went missing"
        )

    def test_the_shared_env_carries_the_data_database(self):
        helpers = (TEMPLATES / "_helpers.tpl").read_text(encoding="utf-8")
        assert "DFE_CLICKHOUSE_DATA_DATABASE" in helpers
