#  Project:      dfe-engine
#  File:         tests/unit/test_keda_shim/test_served_metrics.py
#  Purpose:      The shim pod exports process gauges and one request histogram
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the shim pod's metrics carry, measured in a child process.

OpenTelemetry's global MeterProvider is set once per process and the unit suite
disables the SDK, so the probe builds the pod's app in a fresh interpreter, serves
one poll, and prints the exposition of the registry the OTLP push reads from too.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROBE = "tests/support/keda_shim_metrics_probe.py"
REQUEST_HISTOGRAM = "http_server_request_duration_seconds"
SHIM_SCOPE = 'otel_scope_name="dfe-keda-shim"'


@pytest.fixture(scope="module")
def exposition() -> str:
    """The Prometheus text the probe prints after one poll."""
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
def test_the_process_gauges_come_from_the_shims_own_meter(exposition, gauge):
    rows = _rows(exposition, f"{gauge}{{")

    assert [row for row in rows if SHIM_SCOPE in row], exposition


def test_a_poll_is_recorded_under_its_route_template(exposition):
    counts = _rows(exposition, f"{REQUEST_HISTOGRAM}_count{{")

    assert [row for row in counts if 'http_route="/q/{name}"' in row], exposition
    assert "not-a-catalogued-query" not in exposition


def test_the_request_histogram_is_one_family_from_one_scope(exposition):
    counts = _rows(exposition, f"{REQUEST_HISTOGRAM}_count{{")

    assert len(_rows(exposition, f"# TYPE {REQUEST_HISTOGRAM} ")) == 1
    assert counts
    assert [row for row in counts if SHIM_SCOPE not in row] == []
