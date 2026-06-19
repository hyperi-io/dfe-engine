#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/sigma.py
#  Purpose:      REST API for Sigma rule conversion and source mapping
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Sigma router — rule listing, field mappings, and view generation.

Wraps ``SigmaSourceMapper`` for REST access to Sigma field resolution
and ``SigmaRuleConverter`` for rule listing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.source.registry import SourceNotFoundError

router = APIRouter(prefix="/sigma", tags=["sigma"])


# ── Response models ─────────────────────────────────────────


class FieldMapping(BaseModel):
    """A single Sigma field → column mapping."""

    sigma_field: str
    column_name: str


class SigmaViewResult(BaseModel):
    """Generated Sigma view DDL for a source."""

    source_name: str
    ddl: str | None = Field(description="DDL for the view, or null if no mappings")


class SourceMappingSummary(BaseModel):
    """Sigma mapping summary for a source."""

    source_name: str
    field_count: int
    mappings: list[FieldMapping]


class LogsourceMatch(BaseModel):
    """A source matching a Sigma logsource selector."""

    source: str
    display_name: str = ""


# ── Dependencies ────────────────────────────────────────────


def _get_source_mapper(request: Request):
    """Build a SigmaSourceMapper from app state registries."""
    from dfe_engine.api.deps import _registries
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    source_registry = _registries.get("source")
    if source_registry is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SourceRegistry not configured — required for Sigma mappings",
            },
        )

    fieldmap_registry = _registries.get("fieldmap")
    return SigmaSourceMapper(
        source_registry=source_registry,
        registry=None,
        field_map_registry=fieldmap_registry,
    )


def _source_not_found(source_name: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
    )


# ── Endpoints ───────────────────────────────────────────────


@router.get(
    "/mappings/{source_name}",
    response_model=SourceMappingSummary,
    dependencies=[Depends(require_action(scopes_dict["sigma_read"]))],
)
async def get_field_mappings(
    source_name: str,
    request: Request,
    user: CurrentUser,
    _auth: None = Depends(require_action(scopes_dict["sigma_read"])),
) -> SourceMappingSummary:
    """Get Sigma field mappings for a source."""
    mapper = _get_source_mapper(request)
    try:
        mappings = mapper.get_field_mappings(source_name)
    except (KeyError, FileNotFoundError, SourceNotFoundError):
        raise _source_not_found(source_name)
    items = [FieldMapping(sigma_field=k, column_name=v) for k, v in mappings.items()]
    return SourceMappingSummary(
        source_name=source_name,
        field_count=len(items),
        mappings=items,
    )


@router.post(
    "/views/{source_name}",
    response_model=SigmaViewResult,
    dependencies=[Depends(require_action(scopes_dict["sigma_write"]))],
)
async def generate_sigma_view(
    source_name: str,
    request: Request,
    user: CurrentUser,
    database: str = Query("default", description="Target database"),
    _auth: None = Depends(require_action(scopes_dict["sigma_write"])),
) -> SigmaViewResult:
    """Generate Sigma view DDL for a source.

    Returns the DDL string — does NOT execute it against ClickHouse.
    """
    mapper = _get_source_mapper(request)
    try:
        ddl = mapper.generate_sigma_view(source_name, database)
    except (KeyError, FileNotFoundError, SourceNotFoundError):
        raise _source_not_found(source_name)
    return SigmaViewResult(source_name=source_name, ddl=ddl)


@router.post(
    "/views",
    response_model=list[SigmaViewResult],
    dependencies=[Depends(require_action(scopes_dict["sigma_write"]))],
)
async def generate_all_sigma_views(
    request: Request,
    user: CurrentUser,
    database: str = Query("default", description="Target database"),
    enabled_only: bool = Query(True, description="Only generate for enabled sources"),
    _auth: None = Depends(require_action(scopes_dict["sigma_write"])),
) -> list[SigmaViewResult]:
    """Generate Sigma view DDL for all sources."""
    mapper = _get_source_mapper(request)
    views = mapper.generate_all_sigma_views(database, enabled_only=enabled_only)
    return [SigmaViewResult(source_name=src, ddl=ddl) for src, ddl in views.items()]


@router.get(
    "/logsource",
    response_model=list[LogsourceMatch],
    dependencies=[Depends(require_action(scopes_dict["sigma_read"]))],
)
async def find_sources_for_logsource(
    request: Request,
    user: CurrentUser,
    product: str | None = Query(None),
    category: str | None = Query(None),
    service: str | None = Query(None),
    _auth: None = Depends(require_action(scopes_dict["sigma_read"])),
) -> list[LogsourceMatch]:
    """Find sources matching a Sigma logsource selector."""
    mapper = _get_source_mapper(request)
    sources = mapper.get_sources_for_logsource(
        product=product or "",
        category=category or "",
        service=service or "",
    )
    return [
        LogsourceMatch(
            source=s.source,
            display_name=getattr(s, "display_name", "") or "",
        )
        for s in sources
    ]
