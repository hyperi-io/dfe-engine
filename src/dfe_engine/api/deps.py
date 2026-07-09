"""Shared FastAPI dependencies for DFE Engine API.

Registry singletons are initialized in the lifespan handler and resolved
per-request via ``Depends()``.  Authentication checks four paths in order:

1. OIDC headers (X-Oidc-Subject) — production, Envoy Gateway fronted
2. API key (X-API-Key) — machine-to-machine
3. JWT Bearer — standalone/Docker users
4. Auth disabled — dev/test default, root context
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from scalo.logger import logger

from dfe_engine.auth import AuthContext, AuthorizationError, authorize
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.audit import (
    audit_login_denied,
    audit_login_success,
    audit_permission_denied,
)
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.roles import RoleConfig
from dfe_engine.settings import DFESettings

if TYPE_CHECKING:
    from dfe_engine.auth.jwt_authority import JwtAuthority

# ── Settings ──────────────────────────────────────────────────


def get_app_settings(request: Request) -> DFESettings:
    """Get settings from app state (set in create_app)."""
    return request.app.state.settings


Settings = Annotated[DFESettings, Depends(get_app_settings)]


# ── Registry lifecycle ────────────────────────────────────────

_registries: dict[str, Any] = {}


def bootstrap_registries(settings: DFESettings) -> None:
    """Initialize singleton registries on startup. Called from lifespan."""
    if settings.schemas.schemas_dir:
        from dfe_engine.schema.registry import SchemaRegistry

        _registries["meta_schema"] = SchemaRegistry(schemas_directory=settings.schemas.schemas_dir)

    if settings.source.sources_dir:
        from dfe_engine.source.registry import SourceRegistry

        _registries["source"] = SourceRegistry(sources_directory=settings.source.sources_dir)

    if settings.services.config_yaml_dir:
        from dfe_engine.services.registry import ServiceConfigRegistry

        _registries["service_config"] = ServiceConfigRegistry(
            config_directory=settings.services.config_yaml_dir
        )

    if settings.fieldmap.fieldmaps_dir:
        from dfe_engine.fieldmap.registry import FieldMapRegistry

        _registries["fieldmap"] = FieldMapRegistry(
            field_maps_directory=settings.fieldmap.fieldmaps_dir
        )

    if settings.hunts.rules_dir:
        from dfe_engine.hunts.rule_registry import RuleRegistry

        _registries["rules"] = RuleRegistry(rules_directory=settings.hunts.rules_dir)

    if settings.hunts.hunt_dir:
        from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry

        hunt_dir = settings.hunts.hunt_dir.split(",")[0].strip()
        if hunt_dir:
            _registries["hunt_configs"] = HuntConfigRegistry(hunts_directory=hunt_dir)

    if settings.hunts.alert_destinations_dir:
        from scalo.config import DirectoryConfigStore

        _registries["alert_destinations"] = DirectoryConfigStore(
            directory=settings.hunts.alert_destinations_dir,
            writable=True,
        )

    if settings.deployment.config_dir:
        from dfe_engine.deployment.registry import DeploymentConfigRegistry

        _registries["deployment"] = DeploymentConfigRegistry(
            config_directory=settings.deployment.config_dir
        )


def shutdown_registries() -> None:
    """Cleanup registries on shutdown. Called from lifespan."""
    for _name, reg in list(_registries.items()):
        if hasattr(reg, "close"):
            reg.close()
    _registries.clear()


def get_schema_registry():
    """FastAPI dependency: resolve SchemaRegistry singleton (meta schemas)."""
    reg = _registries.get("meta_schema")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SchemaRegistry not initialized — set DFE_SCHEMAS_DIR (schemas.schemas_dir)",
            },
        )
    return reg


def get_source_registry():
    """FastAPI dependency: resolve SourceRegistry singleton."""
    reg = _registries.get("source")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SourceRegistry not initialized — set DFE_SOURCES_DIR",
            },
        )
    return reg


def get_service_config_registry():
    """FastAPI dependency: resolve ServiceConfigRegistry singleton."""
    reg = _registries.get("service_config")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "ServiceConfigRegistry not initialized"
                " — set DFE_SERVICES_CONFIG_YAML_DIR",
            },
        )
    return reg


def get_field_map_registry():
    """FastAPI dependency: resolve FieldMapRegistry singleton."""
    reg = _registries.get("fieldmap")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "FieldMapRegistry not initialized — set DFE_FIELDMAPS_DIR",
            },
        )
    return reg


def get_alert_destinations_store():
    """FastAPI dependency: resolve alert destinations DirectoryConfigStore."""
    store = _registries.get("alert_destinations")
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "Alert destinations not initialized"
                " — set DFE_HUNTS_ALERT_DESTINATIONS_DIR",
            },
        )
    return store


def get_alert_destinations_store_optional():
    """Optional alert destinations store (for hunt delete cascade)."""
    return _registries.get("alert_destinations")


def get_deployment_config_registry():
    """FastAPI dependency: resolve DeploymentConfigRegistry singleton."""
    reg = _registries.get("deployment")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "DeploymentConfigRegistry not initialized"
                " — set DFE_DEPLOYMENT_CONFIG_DIR",
            },
        )
    return reg


def get_rule_registry():
    """FastAPI dependency: resolve RuleRegistry singleton."""
    reg = _registries.get("rules")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "RuleRegistry not initialized — set DFE_HUNTS_RULES_DIR (hunts.rules_dir)",
            },
        )
    return reg


def get_hunt_config_registry():
    """FastAPI dependency: resolve HuntConfigRegistry singleton."""
    reg = _registries.get("hunt_configs")
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "HuntConfigRegistry not initialized — set DFE_HUNTS_DIR (hunts.hunt_dir)",
            },
        )
    return reg


SchemaReg = Annotated[Any, Depends(get_schema_registry)]
SourceReg = Annotated[Any, Depends(get_source_registry)]
ServiceConfigReg = Annotated[Any, Depends(get_service_config_registry)]
FieldMapReg = Annotated[Any, Depends(get_field_map_registry)]
AlertDestStore = Annotated[Any, Depends(get_alert_destinations_store)]
OptionalAlertDestStore = Annotated[Any | None, Depends(get_alert_destinations_store_optional)]
DeploymentConfigReg = Annotated[Any, Depends(get_deployment_config_registry)]
RuleReg = Annotated[Any, Depends(get_rule_registry)]
HuntConfigReg = Annotated[Any, Depends(get_hunt_config_registry)]


# ── ClickHouse client ─────────────────────────────────────────


def get_clickhouse_client(settings: Settings) -> Any:
    """FastAPI dependency: resolve a pooled ClickHouse client wrapper.

    Returns a ``ClickHouseClientWrapper`` whose ``execute()`` accepts
    clickhouse-connect ``parameters={...}`` server-side binding. Tests
    override this via ``app.dependency_overrides`` to inject a fake client.
    """
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
    from dfe_engine.settings import get_clickhouse_config

    manager = ClickHouseManager.get_instance(get_clickhouse_config(settings))
    return manager.get_clickhouse_client()


ClickHouseClient = Annotated[Any, Depends(get_clickhouse_client)]


# ── Authentication ────────────────────────────────────────────


def _get_client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _resolve_roles_from_groups(
    groups: list[str],
    group_store: GroupStore,
) -> tuple[list[str], list[str]]:
    """Resolve roles and org_ids from a list of group names.

    Looks up each group in the GroupStore and collects its roles.
    Unknown groups are silently skipped (no error — the user just gets
    fewer roles).

    Returns:
        Tuple of (sorted unique roles, org_ids).  org_ids is currently
        always empty — reserved for future multi-tenant scoping.
    """
    roles: set[str] = set()
    for group_name in groups:
        group = group_store.get(group_name)
        if group is not None:
            roles.update(group.roles)
    return sorted(roles), []


def _groups_for_local_account(request: Request, user_id: str) -> list[str]:
    """Load group names from AccountStore for JWT users (legacy tokens without groups claim)."""
    if user_id.startswith("apikey:"):
        return []
    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        return []
    account = account_store.get(user_id)
    return list(account.groups) if account is not None else []


def get_role_config(request: Request) -> RoleConfig:
    """Load the current role definitions from disk (not a stale in-memory snapshot)."""
    role_store = getattr(request.app.state, "role_store", None)
    if role_store is not None:
        try:
            return role_store.load_config()
        except FileNotFoundError:
            pass
    role_config = getattr(request.app.state, "role_config", None)
    if role_config is not None:
        return role_config
    return RoleConfig.load_builtin()


def _groups_from_stores(request: Request, user_id: str) -> list[str]:
    """Group names from GroupStore membership and AccountStore (never JWT)."""
    group_store: GroupStore | None = getattr(request.app.state, "group_store", None)
    if group_store is not None:
        from_membership = sorted(g.name for g in group_store.list() if user_id in g.members)
        if from_membership:
            return from_membership

    account_store = getattr(request.app.state, "account_store", None)
    if account_store is not None:
        account = account_store.get(user_id)
        if account is not None:
            return list(account.groups)
    return []


def _has_local_account(request: Request, user_id: str) -> bool:
    if user_id.startswith("apikey:"):
        return False
    account_store = getattr(request.app.state, "account_store", None)
    return account_store is not None and account_store.get(user_id) is not None


def resolve_live_roles_for_user(
    request: Request,
    user_id: str,
    *,
    fallback_groups: list[str] | None = None,
) -> list[str]:
    """Resolve roles from group membership and role mappings (ignores JWT role claims)."""
    group_store: GroupStore | None = getattr(request.app.state, "group_store", None)
    if group_store is None:
        return []

    if user_id.startswith("apikey:"):
        key_name = user_id.removeprefix("apikey:")
        api_key_store: APIKeyStore | None = getattr(request.app.state, "api_key_store", None)
        groups: list[str] = []
        if api_key_store is not None:
            key = api_key_store.get(key_name)
            if key is not None:
                groups = list(key.groups)
        if not groups:
            groups = list(fallback_groups or [])
        roles, _ = _resolve_roles_from_groups(groups, group_store)
        return roles

    roles = group_store.resolve_roles_for_member(user_id)
    if roles:
        return roles

    groups = _groups_from_stores(request, user_id)
    if not groups and not _has_local_account(request, user_id):
        groups = list(fallback_groups or [])
    if groups:
        resolved, _ = _resolve_roles_from_groups(groups, group_store)
        return resolved
    return []


def resolve_live_groups_for_user(
    request: Request,
    user_id: str,
    *,
    fallback_groups: list[str] | None = None,
) -> list[str]:
    """Return current group names for a user from stores."""
    if user_id.startswith("apikey:"):
        key_name = user_id.removeprefix("apikey:")
        api_key_store: APIKeyStore | None = getattr(request.app.state, "api_key_store", None)
        if api_key_store is not None:
            key = api_key_store.get(key_name)
            if key is not None:
                return list(key.groups)
        return list(fallback_groups or [])

    groups = _groups_from_stores(request, user_id)
    if groups:
        return groups
    if _has_local_account(request, user_id):
        return []
    return list(fallback_groups or [])


def require_local_account_enabled(request: Request, user_id: str) -> None:
    """Reject JWT auth when the backing local account exists and is disabled.

    Skips ``apikey:…`` subjects and usernames with no account record.
    """
    if user_id.startswith("apikey:"):
        return

    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        return

    account = account_store.get(user_id)
    if account is None or account.enabled:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Account disabled"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(request: Request) -> AuthContext:
    """Authenticate the request via one of four paths (checked in order).

    1. OIDC headers (X-Oidc-Subject) — set by Envoy Gateway
    2. API key (X-API-Key) — machine-to-machine
    3. JWT Bearer token — standalone/Docker users
    4. Auth disabled — dev/test root context

    When ``auth.enabled=False`` (dev/test default), returns a root AuthContext
    that bypasses authorization if no credentials are provided.
    """
    settings: DFESettings = request.app.state.settings
    request_id = request.headers.get("X-Request-ID")
    client_ip = _get_client_ip(request)
    user_agent = request.headers.get("User-Agent")

    # ── Path 1: OIDC headers (Envoy Gateway) ────────────────────
    oidc_subject = request.headers.get("X-Oidc-Subject")
    if oidc_subject:
        group_store: GroupStore = request.app.state.group_store
        oidc_email = request.headers.get("X-Oidc-Email") or None
        raw_groups = request.headers.get("X-Oidc-Groups", "")
        groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
        roles, org_ids = _resolve_roles_from_groups(groups, group_store)
        logger.debug("OIDC auth", user_id=oidc_subject, groups=groups, roles=roles)
        audit_login_success(oidc_subject, "oidc", client_ip, roles)

        # JIT provisioning — create shadow account on first OIDC login
        jit = getattr(request.app.state, "jit_provisioner", None)
        if jit:
            try:
                jit.ensure_account(oidc_subject, groups, "oidc")
            except Exception:
                logger.exception("JIT provisioning failed", user_id=oidc_subject)

        return AuthContext(
            user_id=oidc_subject,
            email=oidc_email,
            roles=roles,
            groups=groups,
            org_ids=org_ids,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ── Path 2: API key ─────────────────────────────────────────
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        api_key_store: APIKeyStore = request.app.state.api_key_store
        key_meta = api_key_store.verify(api_key_header)
        if key_meta is None:
            audit_login_denied(
                api_key_header[:16] + "...",
                "api_key",
                client_ip,
                "invalid_key",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": "Invalid API key"},
            )
        group_store = request.app.state.group_store
        roles, org_ids = _resolve_roles_from_groups(key_meta.groups, group_store)
        logger.debug(
            "API key auth",
            key_name=key_meta.name,
            groups=key_meta.groups,
            roles=roles,
        )
        audit_login_success(f"apikey:{key_meta.name}", "api_key", client_ip, roles)
        return AuthContext(
            user_id=f"apikey:{key_meta.name}",
            roles=roles,
            groups=key_meta.groups,
            org_ids=org_ids,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ── Path 3: JWT Bearer token ────────────────────────────────
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]

        from jwt.exceptions import InvalidTokenError

        try:
            payload = request.app.state.jwt_authority.verify(token)
        except InvalidTokenError as e:
            audit_login_denied("unknown", "jwt", client_ip, str(e))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": f"Invalid token: {e}"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        jwt_user_id = payload.get("sub", "")
        jwt_email = payload.get("email") or None
        jwt_groups = payload.get("groups")
        if jwt_groups is None:
            jwt_groups = _groups_for_local_account(request, jwt_user_id)
        elif not isinstance(jwt_groups, list):
            jwt_groups = []
        require_local_account_enabled(request, jwt_user_id)
        live_roles = resolve_live_roles_for_user(request, jwt_user_id, fallback_groups=jwt_groups)
        live_groups = resolve_live_groups_for_user(request, jwt_user_id, fallback_groups=jwt_groups)
        audit_login_success(jwt_user_id, "jwt", client_ip, live_roles)
        return AuthContext(
            org_id=payload.get("org_id", "default"),
            user_id=jwt_user_id,
            email=jwt_email,
            roles=live_roles,
            org_ids=payload.get("org_ids", []),
            groups=live_groups,
            request_id=request_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

    # ── Path 4: Auth disabled (dev/test) ────────────────────────
    if not settings.auth.enabled:
        return AuthContext(org_id="default", user_id="dev", roles=["admin"])

    audit_login_denied("anonymous", "none", client_ip, "no_credentials")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthorized", "message": "Authentication required"},
        headers={"WWW-Authenticate": "Bearer"},
    )


CurrentUser = Annotated[AuthContext, Depends(get_current_user)]


# ── Authorization ─────────────────────────────────────────────


def require_action(action: str):
    """FastAPI dependency factory for RBAC enforcement.

    Usage::

        @router.post("/sources")
        async def create_source(
            user: CurrentUser,
            _auth: None = Depends(require_action("source:write")),
        ): ...
    """

    async def _check(
        request: Request,
        user: AuthContext = Depends(get_current_user),
        settings: DFESettings = Depends(get_app_settings),
    ) -> None:
        role_config = getattr(request.app.state, "role_config", None)
        result = authorize(
            user,
            action,
            enabled=settings.auth.enabled,
            role_config=role_config,
        )
        if not result.allowed:
            audit_permission_denied(user.user_id, action, user.roles, result.reason)
            raise AuthorizationError(f"Action '{action}' denied: {result.reason}")

    return _check


# ── JWT helpers ──────────────────────────────────────────────


# One JwtAuthority per distinct (secrets + jwt) config, reused: the app's
# app.state.jwt_authority and create_access_token resolve the SAME instance (hence
# the same signing key + kid), so a token minted here verifies at the app.
_authority_cache: dict[tuple, JwtAuthority] = {}


def jwt_authority_for(settings: DFESettings) -> JwtAuthority:
    """Get (or build) the ES384 JWT authority for these settings."""
    from dfe_engine.auth.jwt_authority import JwtAuthority
    from dfe_engine.secrets import build_secrets

    s = settings.secrets
    a = settings.api
    cache_key = (
        s.provider,
        s.path,
        s.addr,
        s.mount,
        s.role,
        a.jwt_issuer,
        a.jwt_algorithm,
        a.jwt_key_path,
    )
    authority = _authority_cache.get(cache_key)
    if authority is None:
        authority = JwtAuthority(
            build_secrets(s),
            issuer=a.jwt_issuer,
            algorithm=a.jwt_algorithm,
            key_path=a.jwt_key_path,
            expire_minutes=a.jwt_expire_minutes,
        )
        _authority_cache[cache_key] = authority
    return authority


def create_access_token(
    data: dict,
    settings: DFESettings,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed ES384 DFE identity token (via the JWT authority)."""
    return jwt_authority_for(settings).sign(data, expires_delta=expires_delta)
