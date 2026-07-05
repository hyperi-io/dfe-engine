#  Project:      dfe-engine
#  File:         hunts/alert_hunt_link.py
#  Purpose:      Link alert destinations into hunt YAML alerts.destinations
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Link API-managed alert destinations to hunt YAML ``alerts.destinations``."""

from __future__ import annotations

from typing import Any

from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigRegistry,
)

ALERT_DEST_DATA_KEY = "data"


def add_destination_to_hunt(
    registry: HuntConfigRegistry, hunt_name: str, destination_name: str
) -> None:
    """Append ``destination_name`` to the hunt's ``alerts.destinations`` list (idempotent)."""
    config = registry.get(hunt_name)
    alerts = config.get("alerts")
    if not isinstance(alerts, dict):
        alerts = {}
    destinations_raw = alerts.get("destinations", [])
    destinations = list(destinations_raw) if isinstance(destinations_raw, list) else []
    if destination_name not in destinations:
        destinations.append(destination_name)
    alerts["destinations"] = destinations
    config["alerts"] = alerts
    registry.save(hunt_name, config)


def require_hunt(registry: HuntConfigRegistry, hunt_name: str) -> None:
    """Raise ``HuntConfigNotFoundError`` if the hunt file stem does not exist."""
    registry.get(hunt_name)


def delete_destinations_owned_by_hunt(
    store: Any,
    hunt_registry: HuntConfigRegistry,
    hunt_name: str,
) -> list[str]:
    """Delete destinations owned by ``hunt_name`` when they are not used by other hunts."""
    deleted: list[str] = []
    for dest_name in store.list_tables():
        data = store.get(dest_name, ALERT_DEST_DATA_KEY)
        if not isinstance(data, dict):
            continue
        if data.get("hunt_name") != hunt_name:
            continue
        hunts_using = hunt_registry.hunt_names_referencing_destination(dest_name)
        # Still referenced by another hunt -> only clear our marker; delete when
        # this hunt is the sole (or last) referrer.
        used_by_other = len(hunts_using) > 1 or (
            len(hunts_using) == 1 and hunts_using[0] != hunt_name
        )
        if used_by_other:
            _clear_hunt_name_if_owned(store, dest_name, data, hunt_name)
            continue
        store.delete(dest_name, ALERT_DEST_DATA_KEY)
        deleted.append(dest_name)
    return deleted


def _clear_hunt_name_if_owned(store: Any, dest_name: str, data: dict, hunt_name: str) -> None:
    if data.get("hunt_name") != hunt_name:
        return
    payload = {k: v for k, v in data.items() if k != "hunt_name"}
    store.set(dest_name, ALERT_DEST_DATA_KEY, payload)
