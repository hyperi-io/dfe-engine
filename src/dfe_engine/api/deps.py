"""Shared FastAPI dependencies for DFE Engine API.

Registry singletons are initialized in the lifespan handler and resolved
per-request via ``Depends()``.  Authentication extracts a JWT Bearer token
and returns ``AuthContext`` — the engine's canonical identity model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from dfe_engine.auth import AuthContext, AuthorizationError, authorize
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


async def get_current_user(request: Request) -> AuthContext:
    """Extract and validate JWT Bearer token, returning AuthContext.

    When ``auth.enabled=False`` (dev/test default), returns a root AuthContext
    that bypasses authorization.
    """
    settings: DFESettings = request.app.state.settings

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        if not settings.auth.enabled:
            return AuthContext(org_id="default", user_id="dev", roles=["admin"])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Authorization header required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": f"Invalid token: {e}"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthContext(
        org_id=payload.get("org_id", "default"),
        user_id=payload.get("sub", ""),
        roles=payload.get("roles", []),
        request_id=request.headers.get("X-Request-ID"),
        client_ip=_get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
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
