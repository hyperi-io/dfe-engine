#  Project:      dfe-engine
#  File:         hunt_runner/delivery.py
#  Purpose:      Send a completed fire's matches to the hunt's alert destinations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deliver a completed hunt fire to the destinations its ``alerts`` block names.

One message per fire that wrote rows, however many rows it wrote, so a flood of
matches is one notification and not thousands. The triggers see ``result_count``
only: the runner never reads the matched rows back, so a trigger over a row field
does not fire here.

A delivery failure is logged and counted, never raised. The fire's rows and its
watermark are already committed, and failing the fire would re-run the window
and write the same rows again.
"""

from collections.abc import Callable
from typing import Any

from scalo.logger import logger

from dfe_engine.hunts.alert import (
    AlertConfig,
    AlertDestinationRegistry,
    AlertDispatcher,
    build_alert_config,
)

from .models import HuntSpec

DispatcherFactory = Callable[[AlertConfig], AlertDispatcher]
"""Builds the dispatcher for one fire's config."""


class MatchNotifier:
    """Resolves a hunt's alert destinations and sends one notification per fire.

    Destinations are resolved on every fire, so one added or disabled through the
    API takes effect without a runner restart.

    Args:
        registry: The named destinations a hunt's ``alerts.destinations`` refers to.
        global_channels: Apprise URLs applied to every hunt (``hunts.alert_channels``).
        dispatcher_factory: Builds the dispatcher for a config. Defaults to Apprise.
    """

    def __init__(
        self,
        registry: AlertDestinationRegistry | None = None,
        global_channels: list[str] | None = None,
        dispatcher_factory: DispatcherFactory = AlertDispatcher,
    ) -> None:
        self._registry = registry
        self._global_channels = global_channels or []
        self._dispatcher_factory = dispatcher_factory

    def notify(self, spec: HuntSpec, rows_written: int) -> bool:
        """Send the fire's notification if the hunt asks for one and the fire found something.

        Args:
            spec: The hunt that fired.
            rows_written: Rows the fire wrote across its statements.

        Returns:
            True when a notification was sent.
        """
        if rows_written <= 0 or not (spec.alerts or self._global_channels):
            return False
        try:
            config = build_alert_config(
                {"alerts": spec.alerts},
                global_channels=self._global_channels,
                destination_registry=self._registry,
            )
            if config is None:
                logger.warning("hunt alerts resolve to no destination", hunt_id=spec.hunt_id)
                return False
            return self._dispatcher_factory(config).evaluate_and_send(
                hunt_name=spec.hunt_id,
                customer=", ".join(spec.customers or []),
                rule_name=", ".join(_rule_ids(spec)),
                result_count=rows_written,
            )
        except Exception as exc:
            logger.error("hunt alert delivery failed", hunt_id=spec.hunt_id, error=str(exc))
            return False


def _rule_ids(spec: HuntSpec) -> list[str]:
    """The distinct rules the hunt's statements were compiled from, in order."""
    seen: dict[str, None] = {}
    for statement in spec.queries:
        if statement.rule_id:
            seen[statement.rule_id] = None
    return list(seen)


def alerts_block(definition: dict[str, Any]) -> dict[str, Any]:
    """The hunt YAML's ``alerts`` mapping, or empty when it is absent or not a mapping."""
    alerts = definition.get("alerts")
    return alerts if isinstance(alerts, dict) else {}
