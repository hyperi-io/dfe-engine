"""Sources router — CRUD, pagination, search, sort, bulk operations.

GET    /api/v1/sources                  → Paginated source list
POST   /api/v1/sources                  → Create source
GET    /api/v1/sources/{name}/versions     → Get one version snapshot
PUT    /api/v1/sources/{name}           → Update source
DELETE /api/v1/sources/{name}           → Delete source
POST   /api/v1/sources/bulk             → Bulk enable/disable/delete
POST   /api/v1/sources/seed             → Seed built-in defaults
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, SourceReg, require_action
from dfe_engine.api.errors import MatchConflictErrorResponse, SourceCreateConflictResponse
from dfe_engine.api.pagination import PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.source.models import (
    PaginatedSourceSummaryResponse,
    Source,
    SourceSummaryObject,
    SourceVersionGetResponse,
    SourceWriteRequest,
)
from dfe_engine.source.registry import SourceMatchConflictError, SourceValidationError

router = APIRouter(prefix="/sources", tags=["Sources"])


def _raise_save_validation_http(exc: SourceValidationError) -> NoReturn:
    """Map registry validation errors to HTTP responses (never 500)."""
    if isinstance(exc, SourceMatchConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "match_conflict",
                "message": str(exc),
                "source": exc.source,
                "conflicting_source": exc.conflicting_source,
                "field": exc.field,
                "value": exc.value,
            },
        ) from exc
    raise HTTPException(
        status_code=422,
        detail={
            "code": "validation_error",
            "message": str(exc),
        },
    ) from exc


# ── Response models ──────────────────────────────────────────


class SourceResponse(BaseModel):
    """Full source after create/update."""

    source: str
    message: str = "ok"


class BulkActionRequest(BaseModel):
    """Bulk operation on multiple sources."""

    action: str = Field(description="Action: enable, disable, delete")
    sources: list[str] = Field(description="Source names to act on")


class BulkActionResponse(BaseModel):
    """Result of a bulk operation."""

    action: str
    succeeded: list[str] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(default_factory=list)


class SeedResponse(BaseModel):
    """Result of seeding built-in sources."""

    seeded: int = Field(description="Number of sources seeded")


# ── Endpoints ────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedSourceSummaryResponse,
    dependencies=[Depends(require_action("source:read"))],
)
async def list_sources(
    user: CurrentUser,
    registry: SourceReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in source name and description"),
    enabled: bool | None = Query(None, description="Filter by enabled status"),
    sort_by: str | None = Query(
        None,
        description="Sort field (source, display_name, enabled, current, deployed_version, updated_at)",
    ),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List sources with pagination, search, filtering, and a full object tree."""
    raw_sources = registry.list_sources(enabled_only=bool(enabled))

    if enabled is False:
        raw_sources = [s for s in raw_sources if not s.get("enabled", True)]

    raw_sources = apply_search(raw_sources, search, ["source", "display_name", "description"])
    raw_sources = apply_sort(raw_sources, sort_by, sort_order)

    summaries = [_to_summary(row) for row in raw_sources]
    return PaginatedSourceSummaryResponse.from_summaries(
        summaries, pagination.page, pagination.per_page
    )


@router.post(
    "",
    response_model=SourceResponse,
    status_code=201,
    responses={
        409: {
            "model": SourceCreateConflictResponse,
            "description": (
                "Source name already exists (code conflict), or receiver match duplicates "
                "another enabled source (code match_conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action("source:write"))],
)
async def create_source(
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Create a new source from a flat source definition (initial version ``1.0.0``)."""
    name = body.source

    if not name:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "'source' field is required",
            },
        )

    if registry.source_exists(name):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Source '{name}' already exists",
            },
        )

    try:
        source = registry.create_source_from_write(body, created_by=user.user_id)
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    audit_resource_change(user.user_id, "source", source.source, "created")
    return SourceResponse(source=source.source, message="created")


@router.get(
    "/{name}/versions",
    response_model=SourceVersionGetResponse,
    dependencies=[Depends(require_action("source:read"))],
)
async def get_source_version(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    version: str = Query(..., description="Source version id to return (required)"),
):
    """Get one immutable source version snapshot by id."""
    from dfe_engine.source.registry import SourceNotFoundError

    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        ) from None

    if version not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version}' not found for source '{name}'",
            },
        )

    version_ids = sorted(source.versions.keys())
    return SourceVersionGetResponse(
        source=source.source,
        display_name=source.display_name,
        description=source.description,
        enabled=source.enabled,
        current=source.current,
        deployed_version=source.deployed_version,
        selected=version,
        versions=version_ids,
        version=source.versions[version],
    )


@router.get(
    "/{name}",
    response_model=Source,
    dependencies=[Depends(require_action("source:read"))],
)
async def get_source(name: str, user: CurrentUser, registry: SourceReg):
    """Get a full source definition by name."""
    from dfe_engine.source.registry import SourceNotFoundError

    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        )
    return source


@router.put(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": MatchConflictErrorResponse,
            "description": "Receiver match duplicates another enabled source",
        },
    },
    dependencies=[Depends(require_action("source:write"))],
)
async def update_source(
    name: str,
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Update a source from a flat revision body (appends next major version)."""
    from dfe_engine.source.registry import SourceNotFoundError

    if not registry.source_exists(name):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        )

    try:
        source = registry.update_source_from_write(name, body, created_by=user.user_id)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        ) from None
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    audit_resource_change(user.user_id, "source", source.source, "updated")
    return SourceResponse(source=source.source, message="updated")


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action("source:write"))],
)
async def delete_source(name: str, user: CurrentUser, registry: SourceReg):
    """Delete a source by name."""
    if not registry.source_exists(name):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        )
    registry.delete_source(name)
    audit_resource_change(user.user_id, "source", name, "deleted")


@router.post(
    "/bulk",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action("source:write"))],
)
async def bulk_action(
    body: BulkActionRequest,
    user: CurrentUser,
    registry: SourceReg,
):
    """Perform a bulk action (enable, disable, delete) on multiple sources."""
    if body.action not in ("enable", "disable", "delete"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": f"Unknown action '{body.action}'. Must be: enable, disable, delete",
            },
        )

    succeeded: list[str] = []
    failed: list[dict[str, str]] = []

    for name in body.sources:
        try:
            if body.action == "delete":
                registry.delete_source(name)
            else:
                source = registry.get_source(name)
                source.enabled = body.action == "enable"
                registry.save_source(source, created_by=user.user_id)
            succeeded.append(name)
        except Exception as e:
            failed.append({"source": name, "error": str(e)})

    result = BulkActionResponse(action=body.action, succeeded=succeeded, failed=failed)
    if succeeded:
        audit_resource_change(user.user_id, "source", ",".join(succeeded), body.action)
    return result


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action("source:write"))],
)
async def seed_sources(user: CurrentUser, registry: SourceReg):
    """Seed built-in default source definitions. Non-destructive (skips existing)."""
    count = registry.seed_builtin_sources(overwrite=False)
    audit_resource_change(user.user_id, "source", "all", "seeded")
    return SeedResponse(seeded=count)


# ── Helpers ──────────────────────────────────────────────────


def _to_summary(raw: dict[str, Any]) -> SourceSummaryObject:
    """Convert registry list row to ``SourceSummaryObject``."""
    return SourceSummaryObject(
        name=raw.get("source", ""),
        display_name=raw.get("display_name"),
        description=raw.get("description"),
        enabled=raw.get("enabled", True),
        current=raw.get("current", "1.0.0"),
        deployed_version=raw.get("deployed_version", "1.0.0"),
        versions=list(raw.get("versions") or []),
        updated_at=raw.get("updated_at") or "",
        header_type=raw.get("header_type"),
        has_transform=bool(raw.get("has_transform")),
        has_fetcher=bool(raw.get("has_fetcher")),
        mapping_standards=list(raw.get("mapping_standards") or []),
    )
