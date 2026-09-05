"""Link API-managed alert destinations to hunt YAML ``alerts.destinations``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dfe_engine.hunts.hunt_config_registry import (
    HuntConfigRegistry,
)

if TYPE_CHECKING:
    from dfe_engine.gitcrud.routing import WriteOutcome
    from dfe_engine.hunts.alert import AlertDestination, AlertDestinationRegistry


def add_destination_to_hunt(
    registry: HuntConfigRegistry, hunt_name: str, destination_name: str
) -> WriteOutcome | None:
    """Append ``destination_name`` to the hunt's ``alerts.destinations`` list (idempotent).

    Returns the hunt write's git routing outcome: in production+team the link sits
    on a review branch, so the hunt does not alert that destination until it merges.
    """
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
    return registry.save(hunt_name, config)


def require_hunt(registry: HuntConfigRegistry, hunt_name: str) -> None:
    """Raise ``HuntConfigNotFoundError`` if the hunt file stem does not exist."""
    registry.get(hunt_name)


def delete_destinations_owned_by_hunt(
    registry: AlertDestinationRegistry,
    hunt_registry: HuntConfigRegistry,
    hunt_name: str,
) -> list[str]:
    """Delete destinations owned by ``hunt_name`` when they are not used by other hunts."""
    deleted: list[str] = []
    for dest in registry.list():
        if dest.hunt_name != hunt_name:
            continue
        hunts_using = hunt_registry.hunt_names_referencing_destination(dest.name)
        if len(hunts_using) > 1:
            _clear_hunt_name_if_owned(registry, dest, hunt_name)
            continue
        if len(hunts_using) == 1 and hunts_using[0] != hunt_name:
            _clear_hunt_name_if_owned(registry, dest, hunt_name)
            continue
        registry.remove(dest.name)
        deleted.append(dest.name)
    return deleted


def _clear_hunt_name_if_owned(
    registry: AlertDestinationRegistry,
    dest: AlertDestination,
    hunt_name: str,
) -> None:
    if dest.hunt_name != hunt_name:
        return
    registry.add(dest.model_copy(update={"hunt_name": None}))
