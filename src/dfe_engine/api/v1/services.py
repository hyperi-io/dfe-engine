"""Services router — ServiceConfigRegistry CRUD.

GET    /api/v1/services                        → Paginated list
GET    /api/v1/services/{service}/{instance}   → Full config
PUT    /api/v1/services/{service}/{instance}   → Save config
DELETE /api/v1/services/{service}/{instance}   → Delete config
POST   /api/v1/services/{service}/{instance}/validate  → Dry-run validate
GET    /api/v1/services/{service}/{instance}/history   → Git history
POST   /api/v1/services/seed                   → Seed built-in defaults
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, ServiceConfigReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort

router = APIRouter(prefix="/services", tags=["Services"])


# ── Response models ──────────────────────────────────────────


class ServiceConfigSummary(BaseModel):
    service: str
    instance: str
    updated_at: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class ServiceConfigDetail(BaseModel):
    """Full service config. Inner config is dynamic (schema-less mode)."""

    service: str = Field(description="Service type (e.g. receiver, loader)")
    instance: str = Field(description="Instance name (e.g. production, staging)")
    config: dict[str, Any] = Field(description="Service-specific configuration")


class ConfigHistoryEntry(BaseModel):
    commit: str
    message: str
    author: str
    date: str


class SeedResponse(BaseModel):
    seeded: int


# ── Endpoints ────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedResponse[ServiceConfigSummary],
    dependencies=[Depends(require_action("config:read"))],
)
async def list_service_configs(
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
    dependencies=[Depends(require_action("config:read"))],
)
async def get_service_config(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
) -> ServiceConfigDetail:
    """Get a full service config by service + instance."""
    from dfe_engine.services.registry import ConfigNotFoundError

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
    config_dict = config if isinstance(config, dict) else config.model_dump(mode="json")
    return ServiceConfigDetail(service=service, instance=instance, config=config_dict)


@router.put(
    "/{service}/{instance}",
    dependencies=[Depends(require_action("config:write"))],
)
async def save_service_config(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: ServiceConfigReg,
):
    """Create or update a service config."""
    registry.save_config(
        service=service,
        config=body,
        instance=instance,
        created_by=user.user_id,
    )
    return {"service": service, "instance": instance, "message": "saved"}


@router.delete(
    "/{service}/{instance}",
    status_code=204,
    dependencies=[Depends(require_action("config:write"))],
)
async def delete_service_config(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
):
    """Delete a service config."""
    from dfe_engine.services.registry import ConfigNotFoundError

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
    registry.delete_config(service, instance)


@router.post(
    "/{service}/{instance}/validate",
    response_model=ValidationResult,
    dependencies=[Depends(require_action("config:read"))],
)
async def validate_service_config(
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
    dependencies=[Depends(require_action("config:read"))],
)
async def get_service_config_history(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
    limit: int = Query(10, ge=1, le=100),
):
    """Get git history for a service config (git-backed registries only)."""
    entries = registry.get_config_history(service, instance, limit=limit)
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
    dependencies=[Depends(require_action("config:write"))],
)
async def seed_service_configs(user: CurrentUser, registry: ServiceConfigReg):
    """Seed built-in default service configs. Non-destructive."""
    count = registry.seed_defaults(overwrite=False)
    return SeedResponse(seeded=count)
