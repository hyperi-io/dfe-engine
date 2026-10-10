#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_daemon_metrics.py
#  Purpose:      The daemon exposes one request histogram and one MeterProvider
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the daemon's ``/metrics`` carries, measured in a child process.

OpenTelemetry's global MeterProvider is set once per process, so the probe builds
the daemon's shape in a fresh interpreter: ServiceApp's manager first, then the API
on it, one request, and two lifespans, each running a schema pass.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROBE = "tests/support/daemon_metrics_probe.py"
REQUEST_HISTOGRAM = "http_server_request_duration_seconds"
SCHEMA_GAUGES = (
    "dfe_schema_bootstrap_state",
    "dfe_schema_bootstrap_duration_seconds",
    "dfe_schema_objects_refused",
)


@pytest.fixture(scope="module")
def daemon(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """The probe's report: the exposition, the MeterProviders held, the refusals logged."""
    root = tmp_path_factory.mktemp("daemon-metrics")
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    # The suite disables the SDK for itself; the daemon runs with it on.
    env.pop("OTEL_SDK_DISABLED", None)
    result = subprocess.run(
        [sys.executable, PROBE, str(root)],
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
    return json.loads(result.stdout.strip().splitlines()[-1])


def _rows(exposition: str, prefix: str) -> list[str]:
    return [row for row in exposition.splitlines() if row.startswith(prefix)]


def test_the_request_histogram_is_one_family_from_one_scope(daemon):
    exposition = daemon["exposition"]

    assert len(_rows(exposition, f"# TYPE {REQUEST_HISTOGRAM} ")) == 1
    counts = _rows(exposition, f"{REQUEST_HISTOGRAM}_count{{")
    assert counts, exposition
    assert [row for row in counts if 'otel_scope_name="fastapi"' in row] == []


def test_schema_passes_report_on_the_daemons_own_provider(daemon):
    assert daemon["meter_providers"] == 1
    assert daemon["override_refusals"] == 0


@pytest.mark.parametrize("gauge", SCHEMA_GAUGES)
def test_the_schema_gauges_keep_their_names(daemon, gauge):
    exposition = daemon["exposition"]

    assert _rows(exposition, f"{gauge}{{"), exposition
    assert _rows(exposition, gauge.removeprefix("dfe_")) == []
    assert _rows(exposition, f"dfe_{gauge}") == []
