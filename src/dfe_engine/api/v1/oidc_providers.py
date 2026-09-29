#  Project:      dfe-engine
#  File:         api/v1/oidc_providers.py
#  Purpose:      OIDC provider CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC provider management router -- CRUD, sync, and connectivity test.

POST   /api/v1/auth/oidc-providers                        -> Create provider
GET    /api/v1/auth/oidc-providers                        -> List providers
GET    /api/v1/auth/oidc-providers/{name}                 -> Get provider config
PUT    /api/v1/auth/oidc-providers/{name}                 -> Update provider
DELETE /api/v1/auth/oidc-providers/{name}                 -> Detach provider
POST   /api/v1/auth/oidc-providers/{name}/sync            -> Force group sync
GET    /api/v1/auth/oidc-providers/{name}/test             -> Test directory connectivity
GET    /api/v1/auth/oidc-providers/{name}/verify-login      -> Verify login config

All endpoints require admin role (org:write).

Credentials go IN as values and are written straight to the DfeSecrets seam; the
provider YAML keeps only the store PATH. A response carries the client id, the
secret paths and the env var names, never a secret value.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.oidc.credential_env import is_env_var_name, provider_secret_path
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.store_names import VALID_NAME
from dfe_engine.governance.ch import request_ch_rbac_reconcile

if TYPE_CHECKING:
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.secrets import DfeSecrets

router = APIRouter(prefix="/oidc-providers", tags=["OIDC Providers"])

_ENV_NAME_HELP = (
    "must be an environment variable name such as OKTA_CLIENT_SECRET, not the "
    "credential itself - send the credential in the matching value field instead"
)


def _reject_non_env_name(value: str) -> str:
    """Refuse a credential pasted where an env var NAME belongs."""
    if value and not is_env_var_name(value):
        raise ValueError(_ENV_NAME_HELP)
    return value


# -- Request / Response models --------------------------------


class GroupResolutionRequest(BaseModel):
    mode: Literal["manual", "token_claim", "api"] = Field(
        default="manual",
        description="Group resolution mode: manual, token_claim, api",
    )
    claim_name: str = Field(default="groups", description="Token claim name for group IDs")
    sync_interval: int = Field(default=3600, description="Seconds between API sync cycles")
    enrich_on_login: bool = Field(
        default=False,
        description="Fetch the user's groups from the directory API at each login "
        "(needed by providers with no groups claim, e.g. Google Workspace)",
    )
    service_account_json: str = Field(
        default="",
        description="Google service account JSON. Write-only: it goes to the secret "
        "store and only its path is kept in config.",
    )
    service_account_json_env: str = Field(default="", description="Env var for Google SA JSON")
    admin_email: str = Field(default="", description="Google Workspace admin email")
    domain: str = Field(default="", description="Google Workspace domain")
    tenant_id_env: str = Field(default="", description="Env var for Entra ID tenant ID")
    client_secret: str = Field(
        default="",
        description="Entra ID application client secret. Write-only: it goes to the "
        "secret store and only its path is kept in config.",
    )
    client_secret_env: str = Field(default="", description="Env var for Entra ID client secret")
    api_token: str = Field(
        default="",
        description="Okta API token. Write-only: it goes to the secret store and only "
        "its path is kept in config.",
    )
    api_token_env: str = Field(default="", description="Env var for Okta API token")
    okta_domain: str = Field(default="", description="Okta organisation domain")

    @field_validator(
        "service_account_json_env", "tenant_id_env", "client_secret_env", "api_token_env"
    )
    @classmethod
    def _env_name_only(cls, value: str) -> str:
        return _reject_non_env_name(value)


class CreateProviderRequest(BaseModel):
    name: str = Field(description="Unique provider name (used as filename stem)")
    type: Literal["generic", "google", "entra_id", "okta"] = Field(
        description="Provider type: generic, google, entra_id, okta"
    )
    display_name: str = Field(default="", description="Human-readable label")
    issuer: str = Field(default="", description="OIDC issuer URL")
    client_id: str = Field(
        default="", description="OIDC client ID. Not a secret, so it is stored in config."
    )
    client_id_env: str = Field(default="", description="Env var name for OIDC client ID")
    client_secret: str = Field(
        default="",
        description="RP client secret used in the auth-code exchange. Write-only: it "
        "goes to the secret store and only its path is kept in config.",
    )
    client_secret_env: str = Field(
        default="",
        description="Env var name for the RP client secret used in the auth-code exchange",
    )
    groups: GroupResolutionRequest = Field(default_factory=GroupResolutionRequest)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        """The name becomes a filename stem and a secret-store path segment.

        Refused here, before any secret is written, on the rule the registry keys on.
        """
        if not VALID_NAME.match(value):
            raise ValueError(
                "must start with a letter or digit, contain only letters, digits, "
                "dot, underscore or hyphen, and be at most 128 characters"
            )
        return value

    @field_validator("client_id_env", "client_secret_env")
    @classmethod
    def _env_name_only(cls, value: str) -> str:
        return _reject_non_env_name(value)


class UpdateProviderRequest(BaseModel):
    enabled: bool | None = Field(None, description="Enable or disable the provider")
    display_name: str | None = Field(None, description="Human-readable label")
    client_id: str | None = Field(None, description="OIDC client ID (not a secret)")
    client_id_env: str | None = Field(None, description="Env var name for OIDC client ID")
    client_secret: str | None = Field(
        None,
        description="Rotate the RP client secret. Write-only: it goes to the secret "
        "store and only its path is kept in config.",
    )
    client_secret_env: str | None = Field(None, description="Env var name for the RP client secret")
    groups: GroupResolutionRequest | None = Field(None, description="Group resolution config")

    @field_validator("client_id_env", "client_secret_env")
    @classmethod
    def _env_name_only(cls, value: str | None) -> str | None:
        return value if value is None else _reject_non_env_name(value)


class GroupResolutionResponse(BaseModel):
    mode: str
    claim_name: str
    sync_interval: int
    enrich_on_login: bool
    service_account_json_env: str
    service_account_json_path: str
    admin_email: str
    domain: str
    tenant_id_env: str
    client_secret_env: str
    client_secret_path: str
    api_token_env: str
    api_token_path: str
    okta_domain: str


class ProviderResponse(BaseModel):
    """Provider config -- env var names and secret paths, never a secret value."""

    name: str
    type: str
    enabled: bool
    display_name: str
    issuer: str
    client_id: str
    client_id_env: str
    client_secret_env: str
    client_secret_path: str
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
    groups_skipped: int = Field(
        default=0,
        description="Provider groups left unsynced: an identifier that makes no valid group "
        "name, a name held by a stored group that does not load, or a name held by a stored "
        "group not linked to that provider group. Each is counted on "
        "auth_oidc_sync_groups_skipped_total{reason}.",
    )
    error: str | None


class TestResponse(BaseModel):
    success: bool
    message: str


class LoginConfigCheck(BaseModel):
    """One check in the login-config verification."""

    name: str
    ok: bool
    detail: str


class LoginConfigResponse(BaseModel):
    """Result of verifying a provider's OIDC LOGIN configuration.

    Distinct from ``/{name}/test``, which verifies the group-DIRECTORY
    credentials (Graph / Okta / Google) and is a no-op for a generic provider.
    This verifies the login half: discovery reachable, and the RP client
    credentials present - the things that make ``/auth/oidc/{name}/login``
    actually work.
    """

    ok: bool
    checks: list[LoginConfigCheck]


# -- Helpers --------------------------------------------------


def _provider_to_response(name: str, provider: OIDCProvider) -> ProviderResponse:
    """Convert an OIDCProvider model to a ProviderResponse."""
    p = provider
    return ProviderResponse(
        name=name,
        type=p.type,
        enabled=p.enabled,
        display_name=p.display_name,
        issuer=p.issuer,
        client_id=p.client_id,
        client_id_env=p.client_id_env,
        client_secret_env=p.client_secret_env,
        client_secret_path=p.client_secret_path,
        groups=GroupResolutionResponse(
            mode=p.groups.mode,
            claim_name=p.groups.claim_name,
            sync_interval=p.groups.sync_interval,
            enrich_on_login=p.groups.enrich_on_login,
            service_account_json_env=p.groups.service_account_json_env,
            service_account_json_path=p.groups.service_account_json_path,
            admin_email=p.groups.admin_email,
            domain=p.groups.domain,
            tenant_id_env=p.groups.tenant_id_env,
            client_secret_env=p.groups.client_secret_env,
            client_secret_path=p.groups.client_secret_path,
            api_token_env=p.groups.api_token_env,
            api_token_path=p.groups.api_token_path,
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


def _secrets(request: Request) -> DfeSecrets:
    """The DfeSecrets seam provider credentials are written to and read from."""
    store = getattr(request.app.state, "dfe_secrets", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "secrets_unavailable",
                "message": "no secrets backend is configured; set DFE_SECRETS_PROVIDER",
            },
        )
    return store


def _store_secret(request: Request, name: str, field: str, value: str) -> str:
    """Write one provider secret through the seam and return the path to record."""
    path = provider_secret_path(name, field)
    _secrets(request).put(path, value)
    return path


def _store_group_secrets(
    request: Request, name: str, groups: GroupResolutionRequest
) -> dict[str, str]:
    """Write whichever directory-API secrets the groups block carries.

    Returns only the paths that were written, so a request that omits a value
    leaves the provider's existing path - and its stored secret - alone.
    """
    paths: dict[str, str] = {}
    if groups.service_account_json:
        paths["service_account_json_path"] = _store_secret(
            request, name, "groups_service_account_json", groups.service_account_json
        )
    if groups.client_secret:
        paths["client_secret_path"] = _store_secret(
            request, name, "groups_client_secret", groups.client_secret
        )
    if groups.api_token:
        paths["api_token_path"] = _store_secret(request, name, "groups_api_token", groups.api_token)
    return paths


def _delete_stored_secrets(request: Request, provider: OIDCProvider) -> None:
    """Remove every secret the provider recorded a path for."""
    store = getattr(request.app.state, "dfe_secrets", None)
    if store is None:
        return
    for path in (
        provider.client_secret_path,
        provider.groups.service_account_json_path,
        provider.groups.client_secret_path,
        provider.groups.api_token_path,
    ):
        if path:
            store.delete(path)


def _refresh_rp(request: Request) -> None:
    """Rebuild the relying party so this write takes effect on the login routes.

    The registry is read fresh from YAML on every call, but the RP snapshots the
    enabled providers and their resolved credentials when it is built. Without
    this rebuild a provider created here 404s on ``/auth/oidc/{name}/login``
    until the process restarts. Cheap and non-raising - see
    ``build_relying_party``.
    """
    from dfe_engine.auth.oidc.rp import build_relying_party

    request.app.state.oidc_rp = build_relying_party(
        _get_registry(request), secrets=getattr(request.app.state, "dfe_secrets", None)
    )


# -- Endpoints ------------------------------------------------


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
    """Create a new OIDC provider configuration (admin only).

    Secret values in the body are written to the secret store before the YAML is
    written, so nothing is stored against a name that already belongs to someone
    else's provider.
    """
    from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

    registry = _get_registry(request)
    if registry.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"OIDC provider '{body.name}' already exists"},
        )

    groups_config = GroupResolutionConfig(
        mode=body.groups.mode,
        claim_name=body.groups.claim_name,
        sync_interval=body.groups.sync_interval,
        enrich_on_login=body.groups.enrich_on_login,
        service_account_json_env=body.groups.service_account_json_env,
        admin_email=body.groups.admin_email,
        domain=body.groups.domain,
        tenant_id_env=body.groups.tenant_id_env,
        client_secret_env=body.groups.client_secret_env,
        api_token_env=body.groups.api_token_env,
        okta_domain=body.groups.okta_domain,
        **_store_group_secrets(request, body.name, body.groups),
    )

    client_secret_path = (
        _store_secret(request, body.name, "client_secret", body.client_secret)
        if body.client_secret
        else ""
    )

    provider = OIDCProvider(
        type=body.type,
        enabled=True,
        display_name=body.display_name,
        issuer=body.issuer,
        client_id=body.client_id,
        client_id_env=body.client_id_env,
        client_secret_env=body.client_secret_env,
        client_secret_path=client_secret_path,
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

    _refresh_rp(request)
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
    """Update an OIDC provider configuration (admin only).

    A secret the body omits keeps the path the provider already holds, so an
    update that only flips ``enabled`` does not strand a stored credential.
    """
    from dfe_engine.auth.oidc.models import GroupResolutionConfig

    registry = _get_registry(request)
    current = registry.get(name)
    if current is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )

    update_fields: dict[str, object] = {}
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.display_name is not None:
        update_fields["display_name"] = body.display_name
    if body.client_id is not None:
        update_fields["client_id"] = body.client_id
    if body.client_id_env is not None:
        update_fields["client_id_env"] = body.client_id_env
    if body.client_secret_env is not None:
        update_fields["client_secret_env"] = body.client_secret_env
    if body.client_secret:
        update_fields["client_secret_path"] = _store_secret(
            request, name, "client_secret", body.client_secret
        )
    if body.groups is not None:
        group_paths = {
            "service_account_json_path": current.groups.service_account_json_path,
            "client_secret_path": current.groups.client_secret_path,
            "api_token_path": current.groups.api_token_path,
        }
        group_paths.update(_store_group_secrets(request, name, body.groups))
        update_fields["groups"] = GroupResolutionConfig(
            mode=body.groups.mode,
            claim_name=body.groups.claim_name,
            sync_interval=body.groups.sync_interval,
            enrich_on_login=body.groups.enrich_on_login,
            service_account_json_env=body.groups.service_account_json_env,
            admin_email=body.groups.admin_email,
            domain=body.groups.domain,
            tenant_id_env=body.groups.tenant_id_env,
            client_secret_env=body.groups.client_secret_env,
            api_token_env=body.groups.api_token_env,
            okta_domain=body.groups.okta_domain,
            **group_paths,
        )

    provider = registry.update(name, **update_fields)
    # Picks up an enable/disable flip and a rotated client secret alike.
    _refresh_rp(request)
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

    Does NOT delete groups -- they become orphaned with their source_provider
    still set to the deleted provider name. The provider's stored credentials
    ARE removed: nothing is left that can authenticate as a detached provider.
    """
    from dfe_engine.auth.groups import GroupStore

    registry = _get_registry(request)
    provider = registry.get(name)
    if provider is None:
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
    _delete_stored_secrets(request, provider)
    # A detached provider must stop serving logins immediately, not at restart.
    _refresh_rp(request)
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
    result = await sync_provider(
        name,
        registry,
        group_store,
        secrets=getattr(request.app.state, "dfe_secrets", None),
        metrics=getattr(request.app.state, "oidc_sync_metrics", None),
    )
    # A group the sync created is a new ClickHouse user to provision.
    if result["created"]:
        request_ch_rbac_reconcile(request.app.state)
    audit_resource_change(user.user_id, "oidc_provider", name, "executed")

    return SyncResponse(
        created=result["created"],
        updated=result["updated"],
        total=result["total"],
        groups_skipped=result["groups_skipped"],
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

    adapter = get_adapter(provider, secrets=getattr(request.app.state, "dfe_secrets", None))
    success, message = await adapter.test_connection()
    return TestResponse(success=success, message=message)


@router.get(
    "/{name}/verify-login",
    response_model=LoginConfigResponse,
    dependencies=[Depends(require_action(scopes_dict["oidc_read"]))],
)
async def verify_login_config(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Verify a provider's OIDC LOGIN config (admin only).

    Answers "are the login creds real and is the IdP reachable" WITHOUT
    attempting an interactive login - the gap ``/{name}/test`` leaves for
    generic providers. Checks: the client_id resolves, the client_secret
    resolves, and the issuer's discovery document is reachable and well-formed.
    """
    registry = _get_registry(request)
    provider = registry.get(name)
    if provider is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"OIDC provider '{name}' not found"},
        )

    from dfe_engine.auth.oidc.credential_env import credential_check

    store = getattr(request.app.state, "dfe_secrets", None)
    checks: list[LoginConfigCheck] = []

    # Resolve each credential the way the RP does and report only whether it
    # resolved - the value never enters the response.
    for label, value, secret_path, env_name in (
        ("client_id", provider.client_id, "", provider.client_id_env),
        ("client_secret", "", provider.client_secret_path, provider.client_secret_env),
    ):
        ok, detail = credential_check(
            label, value=value, secret_path=secret_path, env_name=env_name, secrets=store
        )
        checks.append(LoginConfigCheck(name=label, ok=ok, detail=detail))

    # Discovery document: reachable, JSON, and carries the endpoints Authlib needs.
    if not provider.issuer:
        checks.append(LoginConfigCheck(name="discovery", ok=False, detail="no issuer configured"))
    else:
        url = f"{provider.issuer.rstrip('/')}/.well-known/openid-configuration"
        from scalo.http import AsyncHttpClient

        try:
            async with AsyncHttpClient() as client:
                response = await client.get(url)
                data = response.json()
            missing = [
                k for k in ("issuer", "authorization_endpoint", "jwks_uri") if not data.get(k)
            ]
            if missing:
                checks.append(
                    LoginConfigCheck(
                        name="discovery",
                        ok=False,
                        detail=f"discovery reachable but missing {', '.join(missing)}",
                    )
                )
            else:
                checks.append(
                    LoginConfigCheck(name="discovery", ok=True, detail="discovery reachable")
                )
        except Exception as exc:
            checks.append(
                LoginConfigCheck(name="discovery", ok=False, detail=f"discovery failed: {exc}")
            )

    return LoginConfigResponse(ok=all(c.ok for c in checks), checks=checks)
