"""Services router -- ServiceConfigRegistry CRUD.

GET    /api/v1/services                        -> Paginated list
GET    /api/v1/services/{service}/{instance}   -> Full config
PUT    /api/v1/services/{service}/{instance}   -> Save config
DELETE /api/v1/services/{service}/{instance}   -> Delete config
POST   /api/v1/services/{service}/{instance}/validate  -> Dry-run validate
GET    /api/v1/services/{service}/{instance}/history   -> Git history
POST   /api/v1/services/seed                   -> Seed built-in defaults

With gitops on, a write is a deploy-repo commit routed by the deployment's
posture, so a production+team write sits on a review branch and the
``X-DFE-Review-Required`` header says so.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, SecretStr

from dfe_engine.api.deps import CurrentUser, ServiceConfigReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.review import apply_routing_headers, routing_audit_detail
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import contract
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.gitcrud.routing import ReviewRouting
from dfe_engine.services.registry import ConfigNotFoundError, stored_form

router = APIRouter(prefix="/services", tags=["Services"], dependencies=[WRITE_TURN])

_SECRET_MASK = str(SecretStr("x"))
"""pydantic's own placeholder for a set secret, which a write still reads as the mask."""


def _shown(config: Any) -> dict[str, Any]:
    """A stored config as a read shows it, every credential masked as :data:`contract.REDACTED`.

    A typed config's dump masks only what its model declares secret, so a map of
    plain strings such as ``extra_env`` is judged by name as a schema-less one is.
    """
    if isinstance(config, dict):
        return contract.shown_resource(config)
    return contract.shown_resource(_as_redacted(config.model_dump(mode="json")))


def _as_redacted(value: Any) -> Any:
    """``value`` with pydantic's secret placeholder read as the engine's own mask."""
    if isinstance(value, str):
        return contract.REDACTED if value == _SECRET_MASK else value
    if isinstance(value, dict):
        return {key: _as_redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_redacted(item) for item in value]
    return value


# --- Response models ---


class ServiceConfigSummary(BaseModel):
    """One service config instance in a listing."""

    service: str
    instance: str
    updated_at: str | None = None


class ValidationResult(BaseModel):
    """Whether a service config is valid, and its errors."""

    valid: bool
    errors: list[str] = Field(default_factory=list)


class ServiceConfigDetail(BaseModel):
    """Full service config. Inner config is dynamic (schema-less mode)."""

    service: str = Field(description="Service type (e.g. receiver, loader)")
    instance: str = Field(description="Instance name (e.g. production, staging)")
    config: dict[str, Any] = Field(description="Service-specific configuration")


class ConfigHistoryEntry(BaseModel):
    """One commit that changed a service config."""

    commit: str
    message: str
    author: str
    date: str


class SeedResponse(BaseModel):
    """How many default service configs were seeded."""

    seeded: int


# --- Endpoints ---


@router.get(
    "",
    response_model=PaginatedResponse[ServiceConfigSummary],
    dependencies=[Depends(require_action(scopes_dict["service_read"]))],
)
def list_service_configs(
    user: CurrentUser,
    registry: ServiceConfigReg,
    pagination: PaginationParams = Depends(),
    service: str | None = Query(None, description="Filter by service type"),
    search: str | None = Query(None, description="Search in service/instance names"),
    sort_by: str | None = Query(None, description="Sort field (service, instance, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List service configs with optional filtering."""
    raw = registry.list_configs(service=service)
    raw = apply_search(raw, search, ["service", "instance"])
    raw = apply_sort(raw, sort_by, sort_order)
    summaries = [
        ServiceConfigSummary(
            service=item.get("service", ""),
            instance=item.get("instance", "default"),
            updated_at=item.get("updated_at"),
        )
        for item in raw
    ]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get(
    "/{service}/{instance}",
    response_model=ServiceConfigDetail,
    dependencies=[Depends(require_action(scopes_dict["service_read"]))],
)
def get_service_config(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
) -> ServiceConfigDetail:
    """Get a full service config by service + instance, every credential masked."""
    try:
        config = registry.get_config(service, instance)
    except ConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Service config '{service}/{instance}' not found",
            },
        )
    return ServiceConfigDetail(service=service, instance=instance, config=_shown(config))


@router.put(
    "/{service}/{instance}",
    dependencies=[Depends(require_action(scopes_dict["service_write"]))],
)
def save_service_config(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: ServiceConfigReg,
    response: Response,
):
    """Create or update a service config.

    Each top-level section in the body replaces the stored one, and a section the
    body leaves out is kept as stored, so a form that sends some sections cannot
    drop the rest and the secrets in them. A section written as ``null`` is removed.

    A read shows every set secret masked, so a masked value written back keeps the
    secret stored there, matched as the app surface matches one. A mask with nothing
    stored behind it, or in a list entry that cannot be told apart, is a 400
    ``masked_value`` and nothing is saved. A changed field beside a masked secret,
    in the same mapping or with the secret one mapping down, is a 400
    ``credential_reentry_required``: the secret is typed again.
    """
    try:
        current = registry.get_config(service, instance)
    except ConfigNotFoundError:
        stored: dict[str, Any] = {}
        shown: dict[str, Any] = {}
    else:
        stored = stored_form(current)
        shown = _shown(current)
    sent = {key: value for key, value in body.items() if value is not None}
    try:
        restored = contract.restore_masked(_as_redacted(sent), stored, shown=shown)
    except contract.MaskedValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    removed = {key for key, value in body.items() if value is None}
    config = {key: value for key, value in stored.items() if key not in removed} | restored
    routing = ReviewRouting()
    registry.save_config(
        service=service,
        config=config,
        instance=instance,
        created_by=git_author(user),
        routing=routing,
    )
    apply_routing_headers(response, routing)
    audit_resource_change(
        user.user_id,
        "service_config",
        f"{service}/{instance}",
        "updated",
        routing_audit_detail(routing),
    )
    return {"service": service, "instance": instance, "message": "saved"}


@router.delete(
    "/{service}/{instance}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["service_delete"]))],
)
def delete_service_config(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
    response: Response,
):
    """Delete a service config."""
    # Verify existence before delete (delete_config is a no-op on missing)
    try:
        registry.get_config(service, instance)
    except ConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Service config '{service}/{instance}' not found",
            },
        )
    routing = ReviewRouting()
    registry.delete_config(service, instance, created_by=git_author(user), routing=routing)
    apply_routing_headers(response, routing)
    audit_resource_change(
        user.user_id,
        "service_config",
        f"{service}/{instance}",
        "deleted",
        routing_audit_detail(routing),
    )


@router.post(
    "/{service}/{instance}/validate",
    response_model=ValidationResult,
    dependencies=[Depends(require_action(scopes_dict["service_validate"]))],
)
def validate_service_config(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: ServiceConfigReg,
):
    """Dry-run validate a service config without saving."""
    result = registry.validate(service, body)
    return ValidationResult(
        valid=result.valid, errors=result.errors if hasattr(result, "errors") else []
    )


@router.get(
    "/{service}/{instance}/history",
    response_model=list[ConfigHistoryEntry],
    dependencies=[Depends(require_action(scopes_dict["service_read"]))],
)
def get_service_config_history(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
    limit: int = Query(10, ge=1, le=100),
):
    """Get git history for a service config (git-backed registries only)."""
    try:
        entries = registry.get_config_history(service, instance, limit=limit)
    except ValueError as exc:
        # Unknown/schema-less service -> _validate_service raises ValueError.
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": str(exc)},
        ) from exc
    return [
        ConfigHistoryEntry(
            commit=e.get("commit", ""),
            message=e.get("message", ""),
            author=e.get("author", ""),
            date=e.get("date", ""),
        )
        for e in entries
    ]


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action(scopes_dict["service_write"]))],
)
def seed_service_configs(user: CurrentUser, registry: ServiceConfigReg, response: Response):
    """Seed built-in default service configs. Non-destructive."""
    routing = ReviewRouting()
    count = registry.seed_defaults(overwrite=False, created_by=git_author(user), routing=routing)
    apply_routing_headers(response, routing)
    audit_resource_change(
        user.user_id, "service_config", "all", "seeded", routing_audit_detail(routing)
    )
    return SeedResponse(seeded=count)
