#  Project:      dfe-engine
#  File:         api/e2e/seed/metrics.py
#  Purpose:      What an e2e reset did to the deployment's own pools
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The e2e seeders' instruments, as scalo metrics.

A reset rewrites the deployment's own receiver and loader pools rather than
deleting them, and recreates one a run removed. Both are silent in the deploy
repo's history once folded into the reset's single commit, so they are counted
here, landing as ``dfe_e2e_*``. With no backend wired every record call returns
without doing anything, which is the state the unit suite runs in.
"""

import functools
from typing import Any, Literal

POOL_RESETS = "e2e_pool_resets_total"

PoolReset = Literal["restored", "recreated", "removed"]
"""What a reset did to one pool.

- ``restored``: rewrote the deployer's pool to its baseline.
- ``recreated``: wrote the deployer's pool back after a run had removed it.
- ``removed``: undeployed a pool a seed had created.
"""


class SeedMetrics:
    """The seeders' instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._pool_resets = manager.counter(
            POOL_RESETS, "Pools an e2e reset touched, by what it did", ["service", "action"]
        )

    @property
    def enabled(self) -> bool:
        """Whether a backend is wired, so a caller can skip work nothing reads."""
        return self._manager is not None

    def pool_reset(self, service: str, action: PoolReset) -> None:
        """Record what a reset did to one pool."""
        if self._manager is None:
            return
        self._pool_resets.labels(service=service, action=action).inc()


@functools.cache
def create(app_name: str = "dfe-engine") -> SeedMetrics:
    """Build the instrument set on scalo's metrics backend, once per process.

    Every manager scalo builds starts its own exporter, so a second call hands back
    the first set, in the product's own metric namespace.
    """
    from scalo.metrics import create_metrics

    from dfe_engine.deployment_contract import engine_deployment_contract

    return SeedMetrics(
        create_metrics(app_name, metric_prefix=engine_deployment_contract().metric_prefix)
    )


__all__ = ["POOL_RESETS", "PoolReset", "SeedMetrics", "create"]
