"""Field maps router — FieldMapRegistry CRUD.

GET    /api/v1/field-maps                  → Paginated list
GET    /api/v1/field-maps/group            → Paginated list with grouping
POST   /api/v1/field-maps                  → Create field map
GET    /api/v1/field-maps/{standard}       → Default map for standard
GET    /api/v1/field-maps/{standard}/{source} → Source-specific map
DELETE /api/v1/field-maps/{standard}/{source} → Delete source-specific map
POST   /api/v1/field-maps/seed             → Seed built-in defaults
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dfe_engine.api.deps import CurrentUser, FieldMapReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.fieldmap.helpers import group_summaries
from dfe_engine.fieldmap.models import FieldMap

router = APIRouter(prefix="/field-maps", tags=["Field Maps"])


# ── Response models ──────────────────────────────────────────


class FieldMapSummary(BaseModel):
    standard: str
    source: str | None = None
    is_default: bool = False
    version: str | None = None
    mapping_count: int = 0
    updated_at: str | None = None


class FieldMapGroup(BaseModel):
    """One group of field maps, keyed by standard or version."""

    standard: str | None = None
    version: str | None = None
    items: list[FieldMapSummary]
    total: int


class SeedResponse(BaseModel):
    seeded: int


# ── Endpoints ────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedResponse[FieldMapSummary],
    dependencies=[Depends(require_action("config:read"))],
)
async def list_field_maps(
    user: CurrentUser,
    registry: FieldMapReg,
    pagination: PaginationParams = Depends(),
    standard: str | None = Query(None, description="Filter by mapping standard (e.g. sigma, ecs)"),
    search: str | None = Query(None, description="Search in standard/source names"),
    sort_by: str | None = Query(None, description="Sort field (standard, source, mapping_count)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List field maps with optional filtering."""
    raw = registry.list_maps(standard=standard)
    raw = apply_search(raw, search, ["standard", "source"])
    raw = apply_sort(raw, sort_by, sort_order)
    summaries = [
        FieldMapSummary(
            standard=item.get("standard", ""),
            source=item.get("source"),
            is_default=item.get("is_default", False),
            version=item.get("version"),
            mapping_count=item.get("mapping_count", 0),
            updated_at=item.get("updated_at"),
        )
        for item in raw
    ]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get(
    "/group",
    response_model=PaginatedResponse[FieldMapGroup],
    dependencies=[Depends(require_action("config:read"))],
)
async def list_field_maps_grouped(
    user: CurrentUser,
    registry: FieldMapReg,
    pagination: PaginationParams = Depends(),
    standard: str | None = Query(None, description="Filter by mapping standard (e.g. sigma, ecs)"),
    search: str | None = Query(None, description="Search in standard/source names"),
    sort_order: str = Query(
        "asc",
        description="Sort order (asc/desc): orders groups by the group_by key (standard or version), "
        "and items within each group by the other dimension.",
    ),
    group_by: Literal["standard", "version"] = Query(
        ..., description="Group items by standard or version"
    ),
    max_per_group: int = Query(10, ge=-1, le=100, description="Max items per group (-1 for all)"),
):
    """List field maps with grouping by standard or version."""
    raw = registry.list_maps(standard=standard)
    raw = apply_search(raw, search, ["standard", "source"])
    summaries = [
        FieldMapSummary(
            standard=item.get("standard", ""),
            source=item.get("source"),
            is_default=item.get("is_default", False),
            version=item.get("version"),
            mapping_count=item.get("mapping_count", 0),
            updated_at=item.get("updated_at"),
        )
        for item in raw
    ]
    grouped = group_summaries(
        summaries, group_by, max_per_group=max_per_group, sort_order=sort_order
    )
    descending = sort_order in ("desc", "descend")
    grouped = sorted(grouped, key=lambda g: g["key"], reverse=descending)
    groups = [
        FieldMapGroup(
            **{group_by: g["key"]},
            items=g["items"],
            total=g["total"],
        )
        for g in grouped
    ]
    return PaginatedResponse.from_list(groups, pagination.page, pagination.per_page)


@router.post(
    "",
    status_code=201,
    response_model=FieldMap,
    dependencies=[Depends(require_action("config:write"))],
)
async def create_field_map(
    body: FieldMap,
    user: CurrentUser,
    registry: FieldMapReg,
):
    """Create or update a field map."""
    from dfe_engine.fieldmap.registry import FieldMapValidationError

    try:
        fm = registry.save_map(body, created_by=user.user_id)
    except FieldMapValidationError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": str(e),
            },
        )
    return fm


@router.get(
    "/{standard}",
    response_model=FieldMap,
    dependencies=[Depends(require_action("config:read"))],
)
async def get_default_field_map(standard: str, user: CurrentUser, registry: FieldMapReg):
    """Get the default field map for a standard (no source-specific overrides)."""
    from dfe_engine.fieldmap.registry import FieldMapNotFoundError

    try:
        fm = registry.get_map(standard)
    except FieldMapNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Field map for standard '{standard}' not found",
            },
        )
    return fm


@router.get(
    "/{standard}/{source}",
    response_model=FieldMap,
    dependencies=[Depends(require_action("config:read"))],
)
async def get_source_field_map(
    standard: str, source: str, user: CurrentUser, registry: FieldMapReg
):
    """Get the source-specific field map override."""
    from dfe_engine.fieldmap.registry import FieldMapNotFoundError

    try:
        fm = registry.get_map(standard, source=source)
    except FieldMapNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Field map for '{standard}/{source}' not found",
            },
        )
    return fm


@router.delete(
    "/{standard}/{source}",
    status_code=204,
    dependencies=[Depends(require_action("config:write"))],
)
async def delete_source_field_map(
    standard: str, source: str, user: CurrentUser, registry: FieldMapReg
):
    """Delete a source-specific field map override."""
    if not registry.map_exists(standard, source=source):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Field map for '{standard}/{source}' not found",
            },
        )
    registry.delete_map(standard, source=source)


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action("config:write"))],
)
async def seed_field_maps(user: CurrentUser, registry: FieldMapReg):
    """Seed built-in default field maps. Non-destructive."""
    count = registry.seed_defaults(overwrite=False)
    return SeedResponse(seeded=count)
