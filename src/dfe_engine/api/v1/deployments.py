"""Deployments router — DeploymentConfigRegistry CRUD + KEDA sizing.

Manages K8s deployment configuration: replicas, resources, KEDA autoscaling.
Works alongside the services router (runtime config) — together they compose
the full Helm values for each service instance.

GET    /api/v1/deployments                              → Paginated list
GET    /api/v1/deployments/{service}/{instance}         → Full deployment config
PUT    /api/v1/deployments/{service}/{instance}         → Save deployment config
DELETE /api/v1/deployments/{service}/{instance}         → Delete deployment config
POST   /api/v1/deployments/{service}/{instance}/validate → Dry-run validate
GET    /api/v1/deployments/{service}/{instance}/history  → Git history
POST   /api/v1/deployments/{service}/{instance}/size/{size} → Apply t-shirt size
POST   /api/v1/deployments/seed                         → Seed built-in defaults
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, DeploymentConfigReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/deployments", tags=["Deployments"])

# Valid t-shirt sizes
_VALID_SIZES = {"xs", "small", "medium", "large", "xlarge"}


# ── Response models ──────────────────────────────────────────


class DeploymentSummary(BaseModel):
    service: str
    instance: str
    size: str | None = None
    replicas: int | None = None
    keda_enabled: bool | None = None
    updated_at: str | None = None


class SizeResponse(BaseModel):
    service: str
    instance: str
    size: str
    service_config_overrides: dict = Field(
        default_factory=dict,
        description="Service config overrides to apply (e.g. buffer sizes for this size)",
    )


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DeploymentConfigDetail(BaseModel):
    """Full deployment config. Inner config is dynamic (schema-less mode)."""

    service: str = Field(description="Service type")
    instance: str = Field(description="Instance name")
    config: dict[str, Any] = Field(
        description="Deployment-specific configuration (resources, KEDA, etc.)"
    )


class DeploymentHistoryEntry(BaseModel):
    commit: str
    message: str
    author: str
    date: str


class SeedResponse(BaseModel):
    seeded: int


# ── Endpoints ────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedResponse[DeploymentSummary],
    dependencies=[Depends(require_action(scopes_dict["deployment_read"]))],
)
async def list_deployments(
    user: CurrentUser,
    registry: DeploymentConfigReg,
    pagination: PaginationParams = Depends(),
    service: str | None = Query(None, description="Filter by service type"),
    search: str | None = Query(None, description="Search in service/instance names"),
    sort_by: str | None = Query(None, description="Sort field (service, instance, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List deployment configs with optional filtering."""
    raw = registry.list_configs(service=service)
    raw = apply_search(raw, search, ["service", "instance"])
    raw = apply_sort(raw, sort_by, sort_order)
    summaries = [
        DeploymentSummary(
            service=item.get("service", ""),
            instance=item.get("instance", "default"),
            updated_at=item.get("updated_at"),
        )
        for item in raw
    ]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get(
    "/{service}/{instance}",
    response_model=DeploymentConfigDetail,
    dependencies=[Depends(require_action(scopes_dict["deployment_read"]))],
)
async def get_deployment(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: DeploymentConfigReg,
) -> DeploymentConfigDetail:
    """Get a full deployment config by service + instance."""
    from dfe_engine.deployment.registry import DeploymentConfigNotFoundError

    try:
        config = registry.get_config(service, instance)
    except DeploymentConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Deployment config '{service}/{instance}' not found",
            },
        )
    config_dict = config if isinstance(config, dict) else config.model_dump(mode="json")
    return DeploymentConfigDetail(service=service, instance=instance, config=config_dict)


@router.put(
    "/{service}/{instance}",
    dependencies=[Depends(require_action(scopes_dict["deployment_write"]))],
)
async def save_deployment(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: DeploymentConfigReg,
):
    """Create or update a deployment config."""
    registry.save_config(
        service=service,
        config=body,
        instance=instance,
        created_by=user.user_id,
    )
    audit_resource_change(user.user_id, "deployment", f"{service}/{instance}", "updated")
    return {"service": service, "instance": instance, "message": "saved"}


@router.delete(
    "/{service}/{instance}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["deployment_delete"]))],
)
async def delete_deployment(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: DeploymentConfigReg,
):
    """Delete a deployment config."""
    from dfe_engine.deployment.registry import DeploymentConfigNotFoundError

    try:
        registry.get_config(service, instance)
    except DeploymentConfigNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Deployment config '{service}/{instance}' not found",
            },
        )
    registry.delete_config(service, instance)
    audit_resource_change(user.user_id, "deployment", f"{service}/{instance}", "deleted")


@router.post(
    "/{service}/{instance}/validate",
    response_model=ValidationResult,
    dependencies=[Depends(require_action(scopes_dict["deployment_write"]))],
)
async def validate_deployment(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: DeploymentConfigReg,
):
    """Dry-run validate a deployment config without saving."""
    result = registry.validate(service, body)
    return ValidationResult(
        valid=result.valid,
        errors=result.errors if hasattr(result, "errors") else [],
        warnings=result.warnings if hasattr(result, "warnings") else [],
    )


@router.get(
    "/{service}/{instance}/history",
    response_model=list[DeploymentHistoryEntry],
    dependencies=[Depends(require_action(scopes_dict["deployment_read"]))],
)
async def get_deployment_history(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: DeploymentConfigReg,
    limit: int = Query(10, ge=1, le=100),
):
    """Get git history for a deployment config."""
    entries = registry.get_config_history(service, instance, limit=limit)
    return [
        DeploymentHistoryEntry(
            commit=e.get("commit", ""),
            message=e.get("message", ""),
            author=e.get("author", ""),
            date=e.get("date", ""),
        )
        for e in entries
    ]


@router.post(
    "/{service}/{instance}/size/{size}",
    response_model=SizeResponse,
    dependencies=[Depends(require_action(scopes_dict["deployment_write"]))],
)
async def apply_size(
    service: str,
    instance: str,
    size: str,
    user: CurrentUser,
    registry: DeploymentConfigReg,
):
    """Apply a t-shirt size to a deployment config.

    Updates replicas, resources, and KEDA thresholds for the given size.
    Returns any service config overrides that should also be applied
    (e.g. buffer sizes) — the caller may optionally PUT these to
    /api/v1/services/{service}/{instance}.
    """
    if size not in _VALID_SIZES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_size",
                "message": f"Invalid size '{size}'. Valid: {', '.join(sorted(_VALID_SIZES))}",
            },
        )

    try:
        service_overrides = registry.apply_size(
            service=service,
            instance=instance,
            size=size,
            created_by=user.user_id,
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "sizing_error",
                "message": str(e),
            },
        )

    audit_resource_change(user.user_id, "deployment", f"{service}/{instance}", "updated")
    return SizeResponse(
        service=service,
        instance=instance,
        size=size,
        service_config_overrides=service_overrides or {},
    )


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action(scopes_dict["deployment_write"]))],
)
async def seed_deployments(user: CurrentUser, registry: DeploymentConfigReg):
    """Seed built-in default deployment configs. Non-destructive."""
    count = registry.seed_defaults(overwrite=False)
    audit_resource_change(user.user_id, "deployment", "all", "seeded")
    return SeedResponse(seeded=count)
