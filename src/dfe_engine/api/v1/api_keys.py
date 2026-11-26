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
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from scalo.api import ListQuery, Page, ProblemError, list_query, paginate, sort_by_order

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    ReauthGrant,
    bound_org,
    refuse_unknown_orgs,
    refuse_without_grant,
    require_api_key_reauth,
    require_platform_action,
)
from dfe_engine.api.v1.account_groups import check_role_assignment, scope_of
from dfe_engine.auth.audit import audit_api_key_change
from dfe_engine.auth.membership import groups_named
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.reauth import grant_target
from dfe_engine.names import StoreName
from dfe_engine.orgs.role_groups import org_role_breach

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


async def refuse_new_api_key(
    body: CreateAPIKeyRequest, request: Request, user: CurrentUser
) -> None:
    """FastAPI dependency: answer a key create's own refusals before its re-auth grant is asked for."""
    store = request.app.state.api_key_store
    if store.get(body.name) is not None:
        raise ProblemError(409, f"API key '{body.name}' already exists", code="conflict")
    # The key's session takes every role its groups carry, so minting it hands them out.
    named = groups_named(body.groups, request.app.state.group_store.list())
    for group in named:
        check_role_assignment(request, user, group.roles, scope_of(group))
    refuse_unknown_orgs(request, [body.org])
    breach = org_role_breach(
        account_org=body.org, groups=named, home_org=bound_org(account_org="", request=request)
    )
    if breach is not None:
        raise ProblemError(
            422,
            f"API key {body.name!r} is bound to org {body.org!r}, and an org key holds only "
            f"its own org's admin or viewer group, so group {breach!r} is refused",
            code="org_role_only",
        )


async def require_new_api_key_reauth(
    body: CreateAPIKeyRequest, request: Request, user: CurrentUser, reauth: ReauthGrant = ""
) -> None:
    """FastAPI dependency: the caller's re-auth grant for the API key the body creates."""
    target = grant_target(kind="api_key", name=body.name)
    refuse_without_grant(reauth=reauth, request=request, target=target, user=user)


@router.post(
    "",
    response_model=APIKeyCreatedResponse,
    status_code=201,
    dependencies=[
        Depends(require_platform_action(scopes_dict["api_key_write"])),
        Depends(refuse_new_api_key),
        Depends(require_new_api_key_reauth),
    ],
)
async def create_api_key(
    body: CreateAPIKeyRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new API key. Returns the full key exactly once.

    422 ``unknown_org`` when ``org`` names no registered org, ``org_role_only`` when a key bound to an org other than yours names a group outside that org's admin and viewer roles.
    """
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
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
        # Bad or past expires_at: a taken name is refused before the grant is asked for.
        raise ProblemError(400, str(exc), code="validation_error") from exc
    audit_api_key_change(
        user.user_id,
        key_meta.name,
        key_meta.short_token,
        "created",
        {"groups": key_meta.groups, "org": key_meta.org, "expires_at": key_meta.expires_at},
    )
    return APIKeyCreatedResponse(**_response_fields(key_meta), full_key=full_key)


ApiKeysList = Annotated[
    ListQuery, Depends(list_query(order_by=("name", "created_at", "expires_at"), tiebreak="name"))
]


@router.get(
    "",
    response_model=Page[APIKeyResponse],
    dependencies=[Depends(require_platform_action(scopes_dict["api_key_read"]))],
)
async def list_api_keys(
    user: CurrentUser,
    request: Request,
    query: ApiKeysList,
    search: str | None = Query(None, description="Search in key name/description"),
):
    """List API keys, paginated. No key hashes or full keys returned."""
    from dfe_engine.auth.api_keys import APIKeyStore

    store: APIKeyStore = request.app.state.api_key_store
    rows = [APIKeyResponse(**_response_fields(k)).model_dump() for k in store.list()]
    rows = apply_search(rows, search, ["name", "description"])
    rows = sort_by_order(rows, query.order_by)
    summaries = [APIKeyResponse.model_validate(row) for row in rows]
    return paginate(summaries, query, bound={"search": search})


@router.delete(
    "/{short_token}",
    status_code=204,
    dependencies=[
        Depends(require_platform_action(scopes_dict["api_key_delete"])),
        Depends(require_api_key_reauth),
    ],
)
async def revoke_api_key(
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
        raise ProblemError(404, f"No API key with short_token '{short_token}'", code="not_found")
    audit_api_key_change(user.user_id, key_name, short_token, "revoked")


def revoke_org_keys(*, actor: str, org: str, request: Request) -> list[str]:
    """Revoke every API key bound to *org*, once the org itself goes."""
    store = request.app.state.api_key_store
    bound = [key for key in store.list() if key.org == org]
    for key in bound:
        store.revoke(key.short_token)
        audit_api_key_change(actor, key.name, key.short_token, "revoked")
    return [key.name for key in bound]
