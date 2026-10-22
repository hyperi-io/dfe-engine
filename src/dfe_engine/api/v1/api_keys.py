#  Project:      dfe-engine
#  File:         api/v1/api_keys.py
#  Purpose:      API key CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""API key management router -- create, list, revoke.

POST   /api/v1/auth/api-keys                       -> Create key (returns full key ONCE)
GET    /api/v1/auth/api-keys                       -> List keys (short tokens only)
DELETE /api/v1/auth/api-keys/{short_token}          -> Revoke key

Each route needs api_key:read, api_key:write or api_key:delete, and a key may
carry only the roles its creator could hand out. The full key is ONLY returned
in the create response -- it cannot be recovered from stored metadata.

A key's ``org`` binds its sessions to one registered org, the way an account's
does. Empty binds none, and a view held to one org then refuses the key.

A key's ``expires_at`` is the caller's, else ``auth.api_key_default_ttl_days``
from now, and none only when that setting is 0. Once it passes, the key stops
authenticating but stays listed (``expired: true``) until it is revoked.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, refuse_unknown_orgs, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.api.v1.account_groups import check_role_assignment, scope_of
from dfe_engine.auth.audit import audit_api_key_change
from dfe_engine.auth.membership import groups_named
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.names import StoreName

if TYPE_CHECKING:
    from dfe_engine.auth.api_keys import APIKey

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


# -- Request / Response models --------------------------------


_ORG_DESCRIPTION = (
    "The org the key's sessions are bound to: a registered org's name, or empty for "
    "none. A view held to one org runs only for a key bound to an org its groups grant."
)


class CreateAPIKeyRequest(BaseModel):
    """Body for creating an API key."""

    name: StoreName = Field(description="Unique human-readable key name")
    groups: list[str] = Field(default_factory=list, description="RBAC group memberships")
    org: str = Field("", description=_ORG_DESCRIPTION)
    description: str = Field("", description="Optional description")
    expires_at: str | None = Field(
        None,
        description=(
            "Optional ISO-8601 expiry (UTC if no offset given). Omitted, the key "
            "expires after auth.api_key_default_ttl_days (90 unless configured; 0 "
            "there means it never expires)."
        ),
    )


class APIKeyResponse(BaseModel):
    """API key metadata -- key_hash is NEVER included."""

    name: str
    short_token: str
    enabled: bool
    groups: list[str]
    org: str = Field("", description=_ORG_DESCRIPTION)
    description: str
    created_at: str
    expires_at: str | None = Field(None, description="ISO-8601 UTC expiry, or null if never")
    expired: bool = Field(False, description="True once expires_at has passed")


class APIKeyCreatedResponse(APIKeyResponse):
    """Returned exactly once at creation -- includes the full key."""

    full_key: str = Field(description="Full API key (shown once, cannot be recovered)")


# -- Helpers --------------------------------------------------


def _response_fields(key_meta: APIKey) -> dict:
    """Public metadata for one key -- never includes key_hash or the full key."""
    return {
        "name": key_meta.name,
        "short_token": key_meta.short_token,
        "enabled": key_meta.enabled,
        "groups": key_meta.groups,
        "org": key_meta.org,
        "description": key_meta.description,
        "created_at": key_meta.created_at,
        "expires_at": key_meta.expires_at,
        "expired": key_meta.is_expired(),
    }


# -- Endpoints ------------------------------------------------


@router.post(
    "",
    response_model=APIKeyCreatedResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["api_key_write"]))],
)
def create_api_key(
    body: CreateAPIKeyRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new API key. Returns the full key exactly once.

    422 ``unknown_org`` when ``org`` names no registered org.
    """
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    if store.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"API key '{body.name}' already exists"},
        )
    # The key's session takes every role its groups carry, so minting it hands them out.
    for group in groups_named(body.groups, request.app.state.group_store.list()):
        check_role_assignment(request, user, group.roles, scope_of(group))
    refuse_unknown_orgs(request, [body.org])
    expires_at = body.expires_at
    ttl_days = request.app.state.settings.auth.api_key_default_ttl_days
    if expires_at is None and ttl_days > 0:
        expires_at = (datetime.now(UTC) + timedelta(days=ttl_days)).isoformat()
    try:
        key_meta, full_key = store.create(
            body.name,
            groups=body.groups,
            org=body.org,
            description=body.description,
            expires_at=expires_at,
        )
    except ValueError as exc:
        # Bad/past expires_at -- the duplicate-name case is caught above.
        raise HTTPException(
            status_code=400,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    audit_api_key_change(
        user.user_id,
        key_meta.name,
        key_meta.short_token,
        "created",
        {"groups": key_meta.groups, "org": key_meta.org, "expires_at": key_meta.expires_at},
    )
    return APIKeyCreatedResponse(**_response_fields(key_meta), full_key=full_key)


@router.get(
    "",
    response_model=PaginatedResponse[APIKeyResponse],
    dependencies=[Depends(require_action(scopes_dict["api_key_read"]))],
)
def list_api_keys(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in key name/description"),
    sort_by: str | None = Query(None, description="Sort field (name, created_at, expires_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List API keys, paginated. No key hashes or full keys returned."""
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
def revoke_api_key(
    short_token: str,
    user: CurrentUser,
    request: Request,
):
    """Revoke (delete) an API key by short token."""
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    # Read the name first: the revoke deletes the only record that carries it.
    key_name = next((k.name for k in store.list() if k.short_token == short_token), "")
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
    audit_api_key_change(user.user_id, key_name, short_token, "revoked")
