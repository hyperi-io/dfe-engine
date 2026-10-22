"""Alerts router -- alert destination CRUD.

Alert destinations are Apprise notification URLs persisted one YAML file
per destination via the ``AlertDestinationRegistry`` (top-level fields:
``url``, ``description``, ``enabled``, optional ``hunt_name``; the name
comes from the filename). This is the same registry the hunt engine reads
when dispatching alerts, so a destination created here is resolvable by a
hunt straight away.

An Apprise URL carries its own credential -- a Slack token, an SMTP password, a
webhook secret -- and it is whole: the token sits wherever that service's scheme
puts it, so no part of the URL can be shown safely. ``alert:read`` reaches a
viewer role, so a stored URL is masked on every read and the scheme is reported
separately for what a reader actually needs. A PUT that hands the placeholder
back keeps the stored URL, as every other credential write does.

With gitops on, a destination is a deploy-repo document and a write is a commit
routed by the deployment's posture, so a production+team write sits on a review
branch and ``X-DFE-Review-Required`` says so.

GET    /api/v1/alerts/destinations            -> Paginated list
POST   /api/v1/alerts/destinations            -> Create destination
GET    /api/v1/alerts/destinations/{name}     -> Get destination
PUT    /api/v1/alerts/destinations/{name}     -> Update destination
DELETE /api/v1/alerts/destinations/{name}     -> Delete destination
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from scalo.api import scalo_extra

from dfe_engine.api.deps import AlertDestRegistry, CurrentUser, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.review import apply_routing_headers, routing_audit_detail
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import contract
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.gitcrud.routing import ReviewRouting
from dfe_engine.hunts.alert import (
    DESTINATION_NAME_RULE,
    AlertDestinationNameError,
    is_destination_name,
)
from dfe_engine.hunts.alert import AlertDestination as RegistryAlertDestination
from dfe_engine.hunts.alert_hunt_link import (
    add_destination_to_hunt,
    require_hunt,
)
from dfe_engine.hunts.hunt_config_registry import HuntConfigNotFoundError

router = APIRouter(prefix="/alerts", tags=["Alerts"], dependencies=[WRITE_TURN])


def _get_hunt_config_registry_optional() -> Any | None:
    from dfe_engine.api.deps import _registries

    return _registries.get("hunt_configs")


OptionalHuntConfigReg = Annotated[Any | None, Depends(_get_hunt_config_registry_optional)]


# -- Models ----------------------------------------------------


class AlertDestination(BaseModel):
    """An alert destination, with its URL masked on every read."""

    name: str = Field(description="Unique destination identifier (used as filename)")
    url: str = Field(
        description=(
            "Apprise notification URL (slack://, mailto://, etc.). Masked on every read; "
            "write the mask back to keep the stored URL"
        )
    )
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True)
    url_scheme: str = Field(
        default="", description="URL scheme (slack, mailto, etc.); read-only, set from the URL"
    )
    hunt_name: str | None = Field(
        default=None,
        description="Hunt file stem when this destination is owned by a hunt (set via API)",
    )


class AlertDestinationSummary(BaseModel):
    """An alert destination in a listing, without its URL."""

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
def list_destinations(
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
    # No URL in the rows, so sort_by cannot order on another destination's credential.
    raw = [
        {
            "name": dest.name,
            "url_scheme": _url_scheme(dest.url),
            "description": dest.description,
            "enabled": dest.enabled,
            "hunt_name": dest.hunt_name,
        }
        for dest in sorted(registry.list(), key=lambda dest: dest.name)
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
            url_scheme=item.get("url_scheme", ""),
        )
        for item in raw
    ]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


# Declared, since the path alone names the same action as GET /destinations.
@router.post(
    "/destinations",
    response_model=AlertDestination,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["alert_write"]))],
    openapi_extra=scalo_extra(verb="create-destination"),
)
def create_destination(
    body: AlertDestination,
    user: CurrentUser,
    registry: AlertDestRegistry,
    hunt_registry: OptionalHuntConfigReg,
    response: Response,
):
    """Create an alert destination.

    The destination write, and linking it to a hunt, which writes the hunt YAML, are
    each routed to a review branch in production+team, and ``X-DFE-Review-Required``
    says so.
    """
    if not is_destination_name(body.name):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": f"Alert destination name {body.name!r} must be {DESTINATION_NAME_RULE}",
            },
        )
    if body.name in registry:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Alert destination '{body.name}' already exists",
            },
        )
    # Nothing is stored yet, so the mask has nothing to restore from and is refused.
    body.url = _typed_url(body.url, "")
    if body.hunt_name is not None:
        _ensure_hunt_for_link(hunt_registry, body.hunt_name)
    routing = ReviewRouting()
    written = _write_destination(
        registry, body, hunt_name=body.hunt_name, created_by=git_author(user), routing=routing
    )
    if body.hunt_name is not None and hunt_registry is not None:
        routing.record(add_destination_to_hunt(hunt_registry, body.hunt_name, body.name))
    apply_routing_headers(response, routing)
    audit_resource_change(
        user.user_id, "alert_destination", body.name, "created", routing_audit_detail(routing)
    )
    return _destination_from_registry(written)


@router.get(
    "/destinations/{name}",
    response_model=AlertDestination,
    dependencies=[Depends(require_action(scopes_dict["alert_read"]))],
)
def get_destination(name: str, user: CurrentUser, registry: AlertDestRegistry):
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
def update_destination(
    name: str,
    body: AlertDestination,
    user: CurrentUser,
    registry: AlertDestRegistry,
    hunt_registry: OptionalHuntConfigReg,
    response: Response,
):
    """Update an alert destination.

    The destination write, and linking it to a hunt, which writes the hunt YAML, are
    each routed to a review branch in production+team, and ``X-DFE-Review-Required``
    says so.
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
    body.url = _typed_url(body.url, existing.url)
    hunt_to_link = body.hunt_name
    if hunt_to_link is not None:
        _ensure_hunt_for_link(hunt_registry, hunt_to_link)
    stored_hunt_name = hunt_to_link if hunt_to_link is not None else existing.hunt_name
    routing = ReviewRouting()
    written = _write_destination(
        registry, body, hunt_name=stored_hunt_name, created_by=git_author(user), routing=routing
    )
    if hunt_to_link is not None and hunt_registry is not None:
        routing.record(add_destination_to_hunt(hunt_registry, hunt_to_link, name))
    apply_routing_headers(response, routing)
    audit_resource_change(
        user.user_id, "alert_destination", name, "updated", routing_audit_detail(routing)
    )
    return _destination_from_registry(written)


@router.delete(
    "/destinations/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["alert_delete"]))],
)
def delete_destination(
    name: str, user: CurrentUser, registry: AlertDestRegistry, response: Response
):
    """Delete an alert destination.

    A production+team delete is routed to a review branch, so hunts keep alerting the
    destination until that branch is merged, and ``X-DFE-Review-Required`` says so.
    """
    routing = ReviewRouting()
    if not registry.remove(name, created_by=git_author(user), routing=routing):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Alert destination '{name}' not found",
            },
        )
    apply_routing_headers(response, routing)
    audit_resource_change(
        user.user_id, "alert_destination", name, "deleted", routing_audit_detail(routing)
    )


# -- Helpers --------------------------------------------------


def _destination_url_shown(url: str) -> str:
    """A stored destination URL as every read shows it: the whole URL is the credential."""
    return contract.REDACTED if url else ""


def _destination_from_registry(dest: RegistryAlertDestination) -> AlertDestination:
    """The API response model for a stored destination, its URL masked."""
    return AlertDestination(
        name=dest.name,
        url=_destination_url_shown(dest.url),
        description=dest.description,
        enabled=dest.enabled,
        url_scheme=_url_scheme(dest.url),
        hunt_name=dest.hunt_name,
    )


def _typed_url(written: str, stored: str) -> str:
    """The URL to store: the stored one where the mask was written back, else what was written.

    Raises:
        HTTPException: 400 when the mask stands where no URL is stored, so a
            destination is never saved pointing at the placeholder, or masks only
            part of a URL every read shows whole, whether the rest is right or not.
    """
    shown = _destination_url_shown(stored)
    try:
        return contract.restore_masked(
            written, stored or contract.MISSING, path="url", shown=shown or contract.MISSING
        )
    except contract.MaskedValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": exc.code, "message": str(exc)}
        ) from exc


def _write_destination(
    registry,
    dest: AlertDestination,
    *,
    hunt_name: str | None = None,
    created_by: str | None = None,
    routing: ReviewRouting | None = None,
) -> RegistryAlertDestination:
    """Persist a destination via the registry (overwrites if the name exists).

    Returns:
        The destination as written, which a review-routed write has not put on main.

    Raises:
        HTTPException: 422 when the registry cannot store a destination of that name.
    """
    written = RegistryAlertDestination(
        name=dest.name,
        url=dest.url,
        description=dest.description,
        enabled=dest.enabled,
        hunt_name=hunt_name,
    )
    try:
        registry.add(written, created_by=created_by, routing=routing)
    except AlertDestinationNameError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "validation_error", "message": str(exc)}
        ) from exc
    return written


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
