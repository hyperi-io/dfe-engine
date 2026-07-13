#  Project:      dfe-engine
#  File:         keda_shim/__init__.py
#  Purpose:      dfe-keda-shim package - KEDA metrics-api -> ClickHouse query adapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-keda-shim: a tiny, fail-safe, config-driven ClickHouse -> KEDA adapter.

KEDA's ``metrics-api`` scaler polls this shim; the shim runs a config-defined
ClickHouse query and returns one integer. It exists so KEDA can scale on a signal
that lives in ClickHouse (scalo ScalingPressure in HyperDX's otel db, or the hunt
due-count) WITHOUT a Prometheus server AND with a cloud-spend fail-safe: any metric
outage returns the last-good value so scaling FREEZES at current, never runs up.
"""

from .app import create_app
from .shim import QueryShim

__all__ = ["QueryShim", "create_app"]
