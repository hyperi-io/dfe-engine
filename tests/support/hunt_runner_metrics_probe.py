#  Project:      dfe-engine
#  File:         tests/support/hunt_runner_metrics_probe.py
#  Purpose:      The hunt-runner pod's metrics, measured in a process of their own
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The runner's metrics, built in a fresh interpreter and printed as text.

``tests/hunt_runner/test_served_metrics.py`` runs this as a child and reads the
Prometheus exposition it prints. OpenTelemetry's global MeterProvider can be set once
per process, and the unit suite disables the SDK, so the pod's shape needs a process
no other test has touched. The name keeps it out of normal collection.
"""

import sys

from prometheus_client import REGISTRY, generate_latest

from dfe_engine.hunt_runner import metrics


def main() -> int:
    """Build the runner's metrics, record one tick, and print what the registry exposes."""
    metrics.create().tick_completed(0.5)
    sys.stdout.write(generate_latest(REGISTRY).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
