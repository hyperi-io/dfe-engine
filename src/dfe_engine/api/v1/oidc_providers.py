#  Project:      dfe-engine
#  File:         api/v1/oidc_providers.py
#  Purpose:      OIDC provider CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC provider management router — CRUD, sync, and connectivity test.

POST   /api/v1/auth/oidc-providers                        → Create provider
GET    /api/v1/auth/oidc-providers                        → List providers
GET    /api/v1/auth/oidc-providers/{name}                 → Get provider config
PUT    /api/v1/auth/oidc-providers/{name}                 → Update provider
DELETE /api/v1/auth/oidc-providers/{name}                 → Detach provider
POST   /api/v1/auth/oidc-providers/{name}/sync            → Force group sync
GET    /api/v1/auth/oidc-providers/{name}/test             → Test connectivity

All endpoints require admin role (org:write).
Secret env var names are visible but their actual values are never exposed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider

router = APIRouter(prefix="/oidc-providers", tags=["OIDC Providers"])


# ── Request / Response models ────────────────────────────────


class GroupResolutionRequest(BaseModel):
    mode: Literal["manual", "token_claim", "api"] = Field(
        default="manual",
        description="Group resolution mode: manual, token_claim, api",
    )
    claim_name: str = Field(default="groups", description="Token claim name for group IDs")
    sync_interval: int = Field(default=3600, description="Seconds between API sync cycles")
    service_account_json_env: str = Field(default="", description="Env var for Google SA JSON")
    admin_email: str = Field(default="", description="Google Workspace admin email")
    domain: str = Field(default="", description="Google Workspace domain")
    tenant_id_env: str = Field(default="", description="Env var for Entra ID tenant ID")
    client_secret_env: str = Field(default="", description="Env var for Entra ID client secret")
    api_token_env: str = Field(default="", description="Env var for Okta API token")
    okta_domain: str = Field(default="", description="Okta organisation domain")


class CreateProviderRequest(BaseModel):
    name: str = Field(description="Unique provider name (used as filename stem)")
    type: Literal["generic", "google", "entra_id", "okta"] = Field(
        description="Provider type: generic, google, entra_id, okta"
    )
    display_name: str = Field(default="", description="Human-readable label")
    issuer: str = Field(default="", description="OIDC issuer URL")
    client_id_env: str = Field(default="", description="Env var name for OIDC client ID")
    groups: GroupResolutionRequest = Field(default_factory=GroupResolutionRequest)


class UpdateProviderRequest(BaseModel):
    enabled: bool | None = Field(None, description="Enable or disable the provider")
    display_name: str | None = Field(None, description="Human-readable label")
    groups: GroupResolutionRequest | None = Field(None, description="Group resolution config")


class GroupResolutionResponse(BaseModel):
    mode: str
    claim_name: str
    sync_interval: int
    service_account_json_env: str
    admin_email: str
    domain: str
    tenant_id_env: str
    client_secret_env: str
    api_token_env: str
    okta_domain: str


class ProviderResponse(BaseModel):
    """Provider config — env var names are shown, never actual secret values."""

    name: str
    type: str
    enabled: bool
    display_name: str
    issuer: str
    client_id_env: str
    groups: GroupResolutionResponse
    created_at: str
    last_sync_at: str
    last_sync_status: str
    sync_error: str


class OrphanedGroupInfo(BaseModel):
    name: str
    source_provider: str
    roles: list[str]
    member_count: int


class DetachResponse(BaseModel):
    deleted: str
    orphaned_groups: list[OrphanedGroupInfo]


class SyncResponse(BaseModel):
    created: int
    updated: int
    total: int
    error: str | None


class TestResponse(BaseModel):
    success: bool
    message: str


# ── Helpers ──────────────────────────────────────────────────


def _provider_to_response(name: str, provider: OIDCProvider) -> ProviderResponse:
    """Convert an OIDCProvider model to a ProviderResponse."""
    p = provider
    return ProviderResponse(
        name=name,
        type=p.type,
        enabled=p.enabled,
        display_name=p.display_name,
        issuer=p.issuer,
        client_id_env=p.client_id_env,
        groups=GroupResolutionResponse(
            mode=p.groups.mode,
            claim_name=p.groups.claim_name,
            sync_interval=p.groups.sync_interval,
            service_account_json_env=p.groups.service_account_json_env,
            admin_email=p.groups.admin_email,
            domain=p.groups.domain,
            tenant_id_env=p.groups.tenant_id_env,
            client_secret_env=p.groups.client_secret_env,
            api_token_env=p.groups.api_token_env,
            okta_domain=p.groups.okta_domain,
        ),
        created_at=p.created_at,
        last_sync_at=p.last_sync_at,
        last_sync_status=p.last_sync_status,
        sync_error=p.sync_error,
    )


def _get_registry(request: Request):
    """Return OIDCProviderRegistry from app state."""
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    registry: OIDCProviderRegistry = request.app.state.oidc_provider_registry
    return registry


# ── Endpoints ────────────────────────────────────────────────


@router.post(
    "",
    response_model=ProviderResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["oidc_write"]))],
)
async def create_provider(
    body: CreateProviderRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new OIDC provider configuration (admin only)."""
    from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

    registry = _get_registry(request)

    groups_config = GroupResolutionConfig(
        mode=body.groups.mode,
        claim_name=body.groups.claim_name,
        sync_interval=body.groups.sync_interval,
        service_account_json_env=body.groups.service_account_json_env,
        admin_email=body.groups.admin_email,
        domain=body.groups.domain,
        tenant_id_env=body.groups.tenant_id_env,
        client_secret_env=body.groups.client_secret_env,
        api_token_env=body.groups.api_token_env,
        okta_domain=body.groups.okta_domain,
    )

    provider = OIDCProvider(
        type=body.type,
        enabled=True,
        display_name=body.display_name,
        issuer=body.issuer,
        client_id_env=body.client_id_env,
        groups=groups_config,
        created_at=datetime.now(UTC).isoformat(),
    )

    try:
        registry.create(body.name, provider)
    except ValueError:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"OIDC provider '{body.name}' already exists"},
        )

    audit_resource_change(user.user_id, "oidc_provider", body.name, "created")
    return _provider_to_response(body.name, provider)


@router.get(
    "",
    response_model=PaginatedResponse[ProviderResponse],
    dependencies=[Depends(require_action(scopes_dict["oidc_read"]))],
)
async def list_providers(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in provider name/display name"),
    sort_by: str | None = Query(None, description="Sort field (name, type, created_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List OIDC providers (admin only), paginated."""
    registry = _get_registry(request)
    rows = [_provider_to_response(name, p).model_dump() for name, p in registry.list()]
    rows = apply_search(rows, search, ["name", "display_name"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [ProviderResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get(
    "/{name}",
    response_model=ProviderResponse,
    dependencies=[Depends(require_action(scopes_dict["oidc_read"]))],
)
async def get_provider(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single OIDC provider by name (admin only)."""
    registry = _get_registry(request)
    provider = registry.get(name)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )
    return _provider_to_response(name, provider)


@router.put(
    "/{name}",
    response_model=ProviderResponse,
    dependencies=[Depends(require_action(scopes_dict["oidc_write"]))],
)
async def update_provider(
    name: str,
    body: UpdateProviderRequest,
    user: CurrentUser,
    request: Request,
):
    """Update an OIDC provider configuration (admin only)."""
    from dfe_engine.auth.oidc.models import GroupResolutionConfig

    registry = _get_registry(request)
    if registry.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )

    update_fields: dict[str, object] = {}
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.display_name is not None:
        update_fields["display_name"] = body.display_name
    if body.groups is not None:
        update_fields["groups"] = GroupResolutionConfig(
            mode=body.groups.mode,
            claim_name=body.groups.claim_name,
            sync_interval=body.groups.sync_interval,
            service_account_json_env=body.groups.service_account_json_env,
            admin_email=body.groups.admin_email,
            domain=body.groups.domain,
            tenant_id_env=body.groups.tenant_id_env,
            client_secret_env=body.groups.client_secret_env,
            api_token_env=body.groups.api_token_env,
            okta_domain=body.groups.okta_domain,
        )

    provider = registry.update(name, **update_fields)
    audit_resource_change(user.user_id, "oidc_provider", name, "updated")
    return _provider_to_response(name, provider)


@router.delete(
    "/{name}",
    response_model=DetachResponse,
    dependencies=[Depends(require_action(scopes_dict["oidc_delete"]))],
)
async def delete_provider(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Detach an OIDC provider and report orphaned groups (admin only).

    Does NOT delete groups — they become orphaned with their source_provider
    still set to the deleted provider name.
    """
    from dfe_engine.auth.groups import GroupStore

    registry = _get_registry(request)
    if registry.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )

    # Find groups that reference this provider
    group_store: GroupStore = request.app.state.group_store
    orphaned = []
    for group in group_store.list():
        if group.source_provider == name:
            orphaned.append(
                OrphanedGroupInfo(
                    name=group.name,
                    source_provider=group.source_provider,
                    roles=group.roles,
                    member_count=len(group.members),
                )
            )

    registry.delete(name)
    audit_resource_change(user.user_id, "oidc_provider", name, "deleted")

    return DetachResponse(deleted=name, orphaned_groups=orphaned)


@router.post(
    "/{name}/sync",
    response_model=SyncResponse,
    dependencies=[Depends(require_action(scopes_dict["oidc_write"]))],
)
async def sync_provider_groups(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Force group sync for an OIDC provider (admin only)."""
    from dfe_engine.auth.oidc.sync import sync_provider

    registry = _get_registry(request)
    if registry.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )

    group_store = request.app.state.group_store
    result = await sync_provider(name, registry, group_store)
    audit_resource_change(user.user_id, "oidc_provider", name, "executed")

    return SyncResponse(
        created=result["created"],
        updated=result["updated"],
        total=result["total"],
        error=result.get("error"),
    )


@router.get(
    "/{name}/test",
    response_model=TestResponse,
    dependencies=[Depends(require_action(scopes_dict["oidc_read"]))],
)
async def test_provider(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Test connectivity to an OIDC provider (admin only)."""
    from dfe_engine.auth.oidc.adapters import get_adapter

    registry = _get_registry(request)
    provider = registry.get(name)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )

    adapter = get_adapter(provider)
    success, message = await adapter.test_connection()
    return TestResponse(success=success, message=message)
