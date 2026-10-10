#  Project:      dfe-engine
#  File:         schema/metrics.py
#  Purpose:      What the schema bootstrap phase reports about itself
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The schema phase's instruments, as scalo metrics.

A completed ArgoCD Job used to be the operator's record that the schema had been
applied. Once the apply moved inside the engine's lifespan that record went with
it, so the state, the duration and the schemas version are reported here and on
``GET /api/v1/system/schema``.

The daemon registers them on the metrics manager it already serves on ``/metrics``,
so a pass builds no second manager or MeterProvider. They land as ``dfe_schema_*``
whatever namespace that manager carries, the ``dfe`` coming from the deployment
contract. With no manager every record call returns without doing anything, which
is the state the unit suite runs in.
"""

from typing import Any

BOOTSTRAP_STATE = "schema_bootstrap_state"
BOOTSTRAP_DURATION = "schema_bootstrap_duration_seconds"
VERSION_INFO = "schema_version_info"
REFUSED = "schema_objects_refused"


def _registered_name(manager: Any, name: str) -> str:
    """The name to give *manager* so the instrument lands as ``<contract prefix>_<name>``.

    The manager prepends its own namespace, which on the daemon's carries no
    contract prefix, so the prefix is added here unless the manager already adds it.
    """
    from dfe_engine.deployment_contract import engine_deployment_contract

    prefix = engine_deployment_contract().metric_prefix
    if manager.metric_prefix == prefix:
        return name
    return f"{prefix}_{name}"


class SchemaMetrics:
    """The phase's instruments, or a no-op set when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager``. ``None`` means no backend, and every
            record call returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._state = manager.gauge(
            _registered_name(manager, BOOTSTRAP_STATE),
            "0 unknown, 1 converged, 2 failed, 3 running, 4 observed",
        )
        self._duration = manager.gauge(
            _registered_name(manager, BOOTSTRAP_DURATION),
            "Seconds the last schema bootstrap pass took",
        )
        self._version = manager.gauge(
            _registered_name(manager, VERSION_INFO),
            "Always 1; the dfe-schemas release is the label",
            ["schemas_version"],
        )
        self._refused = manager.gauge(
            _registered_name(manager, REFUSED),
            "Objects whose change the last pass declined as drift",
        )

    @property
    def enabled(self) -> bool:
        """Whether a backend is wired, so a caller can skip work nothing reads."""
        return self._manager is not None

    def report(
        self, *, state: int, duration_seconds: float, schemas_version: str, refused: int
    ) -> None:
        """Record the outcome of one pass."""
        if self._manager is None:
            return
        self._state.set(state)
        self._duration.set(duration_seconds)
        self._refused.set(refused)
        if schemas_version:
            self._version.labels(schemas_version=schemas_version).set(1)


def create(app_name: str = "dfe-engine") -> SchemaMetrics:
    """Build the instrument set on a metrics manager of its own.

    For a process that runs one pass and has no manager to share, such as the
    ``dfe auto schema`` command. The daemon passes its own manager instead.
    """
    from scalo.metrics import create_metrics

    from dfe_engine.deployment_contract import engine_deployment_contract

    return SchemaMetrics(
        create_metrics(app_name, metric_prefix=engine_deployment_contract().metric_prefix)
    )


__all__ = [
    "BOOTSTRAP_DURATION",
    "BOOTSTRAP_STATE",
    "REFUSED",
    "VERSION_INFO",
    "SchemaMetrics",
    "create",
]
