#  Project:      dfe-engine
#  File:         tests/support/keda_shim_metrics_probe.py
#  Purpose:      The KEDA shim pod's metrics, measured in a process of their own
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The app the shim pod serves, built in a fresh interpreter, its metrics printed as text.

``tests/unit/test_keda_shim/test_served_metrics.py`` runs this as a child and reads
the Prometheus exposition it prints. OpenTelemetry's global MeterProvider can be set
once per process, and the unit suite disables the SDK, so the pod's shape needs a
process no other test has touched. The name keeps it out of normal collection.
"""

import sys

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, generate_latest

from dfe_engine.keda_shim.cli import served_app
from dfe_engine.settings import DFESettings

# An unknown query name is answered 0 without reaching ClickHouse.
POLL = "/q/not-a-catalogued-query"


def main() -> int:
    """Serve one poll through the pod's app and print what the metrics registry exposes."""
    with TestClient(served_app(DFESettings(env="test"))) as client:
        client.get(POLL)
    sys.stdout.write(generate_latest(REGISTRY).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
