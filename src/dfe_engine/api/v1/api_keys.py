#  Project:      dfe-engine
#  File:         api/v1/api_keys.py
#  Purpose:      API key CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""API key management router — create, list, revoke.

POST   /api/v1/auth/api-keys                       → Create key (returns full key ONCE)
GET    /api/v1/auth/api-keys                       → List keys (short tokens only)
DELETE /api/v1/auth/api-keys/{short_token}          → Revoke key

All endpoints require admin role (org:write).
The full key is ONLY returned in the create response — it cannot be
recovered from stored metadata.

A key may carry an optional ``expires_at``; once it passes, the key stops
authenticating but stays listed (``expired: true``) until it is revoked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth.rbac_scopes import scopes_dict

if TYPE_CHECKING:
    from dfe_engine.auth.api_keys import APIKey

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


# ── Request / Response models ────────────────────────────────


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(description="Unique human-readable key name")
    groups: list[str] = Field(default_factory=list, description="RBAC group memberships")
    description: str = Field("", description="Optional description")
    expires_at: str | None = Field(
        None,
        description="Optional ISO-8601 expiry (UTC if no offset given); omit for a key that never expires",
    )


class APIKeyResponse(BaseModel):
    """API key metadata — key_hash is NEVER included."""

    name: str
    short_token: str
    enabled: bool
    groups: list[str]
    description: str
    created_at: str
    expires_at: str | None = Field(None, description="ISO-8601 UTC expiry, or null if never")
    expired: bool = Field(False, description="True once expires_at has passed")


class APIKeyCreatedResponse(APIKeyResponse):
    """Returned exactly once at creation — includes the full key."""

    full_key: str = Field(description="Full API key (shown once, cannot be recovered)")


# ── Helpers ──────────────────────────────────────────────────


def _response_fields(key_meta: APIKey) -> dict:
    """Public metadata for one key — never includes key_hash or the full key."""
    return {
        "name": key_meta.name,
        "short_token": key_meta.short_token,
        "enabled": key_meta.enabled,
        "groups": key_meta.groups,
        "description": key_meta.description,
        "created_at": key_meta.created_at,
        "expires_at": key_meta.expires_at,
        "expired": key_meta.is_expired(),
    }


# ── Endpoints ────────────────────────────────────────────────


@router.post(
    "",
    response_model=APIKeyCreatedResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["api_key_write"]))],
)
async def create_api_key(
    body: CreateAPIKeyRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new API key (admin only). Returns the full key exactly once."""
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    if store.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"API key '{body.name}' already exists"},
        )
    try:
        key_meta, full_key = store.create(
            body.name,
            groups=body.groups,
            description=body.description,
            expires_at=body.expires_at,
        )
    except ValueError as exc:
        # Bad/past expires_at — the duplicate-name case is caught above.
        raise HTTPException(
            status_code=400,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    return APIKeyCreatedResponse(**_response_fields(key_meta), full_key=full_key)


@router.get(
    "",
    response_model=PaginatedResponse[APIKeyResponse],
    dependencies=[Depends(require_action(scopes_dict["api_key_read"]))],
)
async def list_api_keys(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in key name/description"),
    sort_by: str | None = Query(None, description="Sort field (name, created_at, expires_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List API keys (admin only), paginated. No key hashes or full keys returned."""
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    rows = [APIKeyResponse(**_response_fields(k)).model_dump() for k in store.list()]
    rows = apply_search(rows, search, ["name", "description"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [APIKeyResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.delete(
    "/{short_token}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["api_key_delete"]))],
)
async def revoke_api_key(
    short_token: str,
    user: CurrentUser,
    request: Request,
):
    """Revoke (delete) an API key by short token (admin only)."""
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    try:
        store.revoke(short_token)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"No API key with short_token '{short_token}'",
            },
        )
