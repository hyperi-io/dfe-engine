#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_served_metrics.py
#  Purpose:      The runner pod exports process gauges unprefixed beside its dfe_ metrics
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the runner pod's metrics carry, measured in a child process.

OpenTelemetry's global MeterProvider is set once per process and the unit suite
disables the SDK, so the probe builds the runner's metrics in a fresh interpreter and
prints the exposition of the registry the OTLP push reads from too.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROBE = "tests/support/hunt_runner_metrics_probe.py"
RUNNER_SCOPE = 'otel_scope_name="dfe-hunt-runner"'


@pytest.fixture(scope="module")
def exposition() -> str:
    """The Prometheus text the probe prints after one tick."""
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    # The suite disables the SDK for itself; the pod runs with it on.
    env.pop("OTEL_SDK_DISABLED", None)
    # A blank endpoint turns the OTLP push off, so the probe never dials a collector.
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
    result = subprocess.run(
        [sys.executable, PROBE],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    return result.stdout


def _rows(exposition: str, prefix: str) -> list[str]:
    return [row for row in exposition.splitlines() if row.startswith(prefix)]


@pytest.mark.parametrize(
    "gauge",
    ["process_cpu_seconds_total", "process_resident_memory_bytes", "process_virtual_memory_bytes"],
)
def test_the_process_gauges_are_unprefixed_from_the_runners_own_meter(exposition, gauge):
    assert [row for row in _rows(exposition, f"{gauge}{{") if RUNNER_SCOPE in row], exposition
    assert _rows(exposition, f"dfe_{gauge}") == [], exposition


def test_the_runners_own_metrics_keep_the_product_prefix(exposition):
    assert [
        row
        for row in _rows(exposition, "dfe_hunt_tick_duration_seconds_count{")
        if RUNNER_SCOPE in row
    ], exposition
