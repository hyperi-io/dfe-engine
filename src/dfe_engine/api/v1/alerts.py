"""Alerts router -- alert destination CRUD.

Alert destinations are Apprise notification URLs persisted one YAML file
per destination via the ``AlertDestinationRegistry`` (top-level fields:
``url``, ``description``, ``enabled``, optional ``hunt_name``; the name
comes from the filename). This is the same registry the hunt engine reads
when dispatching alerts, so a destination created here is resolvable by a
hunt straight away.

GET    /api/v1/alerts/destinations            -> Paginated list
POST   /api/v1/alerts/destinations            -> Create destination
GET    /api/v1/alerts/destinations/{name}     -> Get destination
PUT    /api/v1/alerts/destinations/{name}     -> Update destination
DELETE /api/v1/alerts/destinations/{name}     -> Delete destination
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from dfe_engine.api.deps import AlertDestRegistry, CurrentUser, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.review import apply_review_headers
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.hunts.alert import AlertDestination as RegistryAlertDestination
from dfe_engine.hunts.alert_hunt_link import (
    add_destination_to_hunt,
    require_hunt,
)
from dfe_engine.hunts.hunt_config_registry import HuntConfigNotFoundError

router = APIRouter(prefix="/alerts", tags=["Alerts"])


def _get_hunt_config_registry_optional() -> Any | None:
    from dfe_engine.api.deps import _registries

    return _registries.get("hunt_configs")


OptionalHuntConfigReg = Annotated[Any | None, Depends(_get_hunt_config_registry_optional)]


# -- Models ----------------------------------------------------


class AlertDestination(BaseModel):
    name: str = Field(description="Unique destination identifier (used as filename)")
    url: str = Field(description="Apprise notification URL (slack://, mailto://, etc.)")
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True)
    hunt_name: str | None = Field(
        default=None,
        description="Hunt file stem when this destination is owned by a hunt (set via API)",
    )


class AlertDestinationSummary(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    url_scheme: str = Field(default="", description="URL scheme (slack, mailto, etc.)")


# -- Endpoints ------------------------------------------------


@router.get(
    "/destinations",
    response_model=PaginatedResponse[AlertDestinationSummary],
    dependencies=[Depends(require_action(scopes_dict["alert_read"]))],
)
async def list_destinations(
    user: CurrentUser,
    registry: AlertDestRegistry,
    hunt_registry: OptionalHuntConfigReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in name/description"),
    hunt: str | None = Query(
        None,
        description="Hunt file name (YAML stem); only destinations referenced in that hunt's alerts",
    ),
    sort_by: str | None = Query(None, description="Sort field (name, enabled)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List alert destinations."""
    raw = [
        {
            "name": dest.name,
            "url": dest.url,
            "description": dest.description,
            "enabled": dest.enabled,
            "hunt_name": dest.hunt_name,
        }
        for dest in registry.list()
    ]

    if hunt is not None:
        if hunt_registry is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "not_configured",
                    "message": "Hunt config registry not initialized -- set DFE_HUNTS_DIR (hunts.hunt_dir)",
                },
            )
        try:
            allowed = set(hunt_registry.alert_destination_names_for_hunt(hunt))
        except HuntConfigNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Hunt '{hunt}' not found"},
            ) from None
        raw = [item for item in raw if item.get("name") in allowed]

    raw = apply_search(raw, search, ["name", "description"])
    raw = apply_sort(raw, sort_by, sort_order)

    summaries = [
        AlertDestinationSummary(
            name=item.get("name", ""),
            description=item.get("description", ""),
            enabled=item.get("enabled", True),
            url_scheme=_url_scheme(item.get("url", "")),
        )
        for item in raw
    ]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.post(
    "/destinations",
    response_model=AlertDestination,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["alert_write"]))],
)
async def create_destination(
    body: AlertDestination,
    user: CurrentUser,
    registry: AlertDestRegistry,
    hunt_registry: OptionalHuntConfigReg,
    response: Response,
):
    """Create an alert destination.

    Linking it to a hunt writes the hunt YAML, so in production+team that link is
    routed to a review branch and ``X-DFE-Review-Required`` says so.
    """
    if body.name in registry:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Alert destination '{body.name}' already exists",
            },
        )
    if body.hunt_name is not None:
        _ensure_hunt_for_link(hunt_registry, body.hunt_name)
    _write_destination(registry, body, hunt_name=body.hunt_name)
    if body.hunt_name is not None and hunt_registry is not None:
        apply_review_headers(
            response, add_destination_to_hunt(hunt_registry, body.hunt_name, body.name)
        )
    audit_resource_change(user.user_id, "alert_destination", body.name, "created")
    return _destination_from_registry(registry.get(body.name))


@router.get(
    "/destinations/{name}",
    response_model=AlertDestination,
    dependencies=[Depends(require_action(scopes_dict["alert_read"]))],
)
async def get_destination(name: str, user: CurrentUser, registry: AlertDestRegistry):
    """Get an alert destination by name."""
    if name not in registry:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Alert destination '{name}' not found",
            },
        )
    return _destination_from_registry(registry.get(name))


@router.put(
    "/destinations/{name}",
    response_model=AlertDestination,
    dependencies=[Depends(require_action(scopes_dict["alert_write"]))],
)
async def update_destination(
    name: str,
    body: AlertDestination,
    user: CurrentUser,
    registry: AlertDestRegistry,
    hunt_registry: OptionalHuntConfigReg,
    response: Response,
):
    """Update an alert destination.

    Linking it to a hunt writes the hunt YAML, so in production+team that link is
    routed to a review branch and ``X-DFE-Review-Required`` says so.
    """
    if name not in registry:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Alert destination '{name}' not found",
            },
        )
    existing = registry.get(name)
    body.name = name
    hunt_to_link = body.hunt_name
    if hunt_to_link is not None:
        _ensure_hunt_for_link(hunt_registry, hunt_to_link)
    stored_hunt_name = hunt_to_link if hunt_to_link is not None else existing.hunt_name
    _write_destination(registry, body, hunt_name=stored_hunt_name)
    if hunt_to_link is not None and hunt_registry is not None:
        apply_review_headers(response, add_destination_to_hunt(hunt_registry, hunt_to_link, name))
    audit_resource_change(user.user_id, "alert_destination", name, "updated")
    return _destination_from_registry(registry.get(name))


@router.delete(
    "/destinations/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["alert_delete"]))],
)
async def delete_destination(name: str, user: CurrentUser, registry: AlertDestRegistry):
    """Delete an alert destination."""
    if not registry.remove(name):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Alert destination '{name}' not found",
            },
        )
    audit_resource_change(user.user_id, "alert_destination", name, "deleted")


# -- Helpers --------------------------------------------------


def _destination_from_registry(dest: RegistryAlertDestination) -> AlertDestination:
    """Convert a registry destination model to the API response model."""
    return AlertDestination(
        name=dest.name,
        url=dest.url,
        description=dest.description,
        enabled=dest.enabled,
        hunt_name=dest.hunt_name,
    )


def _write_destination(
    registry,
    dest: AlertDestination,
    *,
    hunt_name: str | None = None,
) -> None:
    """Persist a destination via the registry (overwrites if the name exists)."""
    registry.add(
        RegistryAlertDestination(
            name=dest.name,
            url=dest.url,
            description=dest.description,
            enabled=dest.enabled,
            hunt_name=hunt_name,
        )
    )


def _ensure_hunt_for_link(hunt_registry: Any | None, hunt_name: str) -> None:
    if hunt_registry is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "Hunt config registry not initialized -- set DFE_HUNTS_DIR (hunts.hunt_dir)",
            },
        )
    try:
        require_hunt(hunt_registry, hunt_name)
    except HuntConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Hunt '{hunt_name}' not found"},
        ) from None


def _url_scheme(url: str) -> str:
    """Extract scheme from Apprise URL (e.g. 'slack' from 'slack://...')."""
    if "://" in url:
        return url.split("://")[0]
    return ""
