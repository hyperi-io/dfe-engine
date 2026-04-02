"""Shared FastAPI dependencies for DFE Engine API.

Registry singletons are initialized in the lifespan handler and resolved
per-request via ``Depends()``.  Authentication checks four paths in order:

1. OIDC headers (X-Oidc-Subject) — production, Envoy Gateway fronted
2. API key (X-API-Key) — machine-to-machine
3. JWT Bearer — standalone/Docker users
4. Auth disabled — dev/test default, root context
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from hyperi_pylib.logger import logger

from dfe_engine.auth import AuthContext, AuthorizationError, authorize
from dfe_engine.auth.api_keys import APIKeyStore
from dfe_engine.auth.audit import (
    audit_login_denied,
    audit_login_success,
    audit_permission_denied,
)
from dfe_engine.auth.groups import GroupStore
from dfe_engine.settings import DFESettings

# ── Settings ──────────────────────────────────────────────────


def get_app_settings(request: Request) -> DFESettings:
    """Get settings from app state (set in create_app)."""
    return request.app.state.settings


Settings = Annotated[DFESettings, Depends(get_app_settings)]


# ── Registry lifecycle ────────────────────────────────────────

_registries: dict[str, Any] = {}


def bootstrap_registries(settings: DFESettings) -> None:
    """Initialize singleton registries on startup. Called from lifespan."""
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

    if settings.hunts.alert_destinations_dir:
        from hyperi_pylib.config import DirectoryConfigStore

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


SourceReg = Annotated[Any, Depends(get_source_registry)]
ServiceConfigReg = Annotated[Any, Depends(get_service_config_registry)]
FieldMapReg = Annotated[Any, Depends(get_field_map_registry)]
AlertDestStore = Annotated[Any, Depends(get_alert_destinations_store)]
DeploymentConfigReg = Annotated[Any, Depends(get_deployment_config_registry)]


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

        import jwt
        from jwt.exceptions import InvalidTokenError

        try:
            payload = jwt.decode(
                token,
                settings.api.jwt_secret,
                algorithms=[settings.api.jwt_algorithm],
            )
        except InvalidTokenError as e:
            audit_login_denied("unknown", "jwt", client_ip, str(e))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "unauthorized", "message": f"Invalid token: {e}"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        jwt_user_id = payload.get("sub", "")
        jwt_roles = payload.get("roles", [])
        audit_login_success(jwt_user_id, "jwt", client_ip, jwt_roles)
        return AuthContext(
            org_id=payload.get("org_id", "default"),
            user_id=jwt_user_id,
            roles=jwt_roles,
            org_ids=payload.get("org_ids", []),
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
        user: AuthContext = Depends(get_current_user),
        settings: DFESettings = Depends(get_app_settings),
    ) -> None:
        result = authorize(user, action, enabled=settings.auth.enabled)
        if not result.allowed:
            audit_permission_denied(user.user_id, action, user.roles, result.reason)
            raise AuthorizationError(f"Action '{action}' denied: {result.reason}")

    return _check


# ── JWT helpers ──────────────────────────────────────────────


def create_access_token(
    data: dict,
    settings: DFESettings,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT access token."""
    import jwt

    to_encode = data.copy()
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=settings.api.jwt_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.api.jwt_secret, algorithm=settings.api.jwt_algorithm)
