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
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


# ── Request / Response models ────────────────────────────────


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(description="Unique human-readable key name")
    groups: list[str] = Field(default_factory=list, description="RBAC group memberships")
    description: str = Field("", description="Optional description")


class APIKeyResponse(BaseModel):
    """API key metadata — key_hash is NEVER included."""

    name: str
    short_token: str
    enabled: bool
    groups: list[str]
    description: str
    created_at: str


class APIKeyCreatedResponse(APIKeyResponse):
    """Returned exactly once at creation — includes the full key."""

    full_key: str = Field(description="Full API key (shown once, cannot be recovered)")


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
    key_meta, full_key = store.create(body.name, groups=body.groups, description=body.description)
    return APIKeyCreatedResponse(
        name=key_meta.name,
        short_token=key_meta.short_token,
        enabled=key_meta.enabled,
        groups=key_meta.groups,
        description=key_meta.description,
        created_at=key_meta.created_at,
        full_key=full_key,
    )


@router.get(
    "",
    response_model=list[APIKeyResponse],
    dependencies=[Depends(require_action(scopes_dict["api_key_read"]))],
)
async def list_api_keys(
    user: CurrentUser,
    request: Request,
):
    """List all API keys (admin only). No key hashes or full keys returned."""
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    return [
        APIKeyResponse(
            name=k.name,
            short_token=k.short_token,
            enabled=k.enabled,
            groups=k.groups,
            description=k.description,
            created_at=k.created_at,
        )
        for k in store.list()
    ]


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
