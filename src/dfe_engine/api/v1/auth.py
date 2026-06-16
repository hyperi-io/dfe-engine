"""Auth router — login, token refresh, user info, permissions.

POST /api/v1/auth/login      → JWT token (LocalAuthProvider)
POST /api/v1/auth/refresh     → Refreshed JWT token
GET  /api/v1/auth/me          → Current user info
GET  /api/v1/auth/permissions → Resolved permissions for current user's roles
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    create_access_token,
)
from dfe_engine.auth.local_provider import LocalAuthProvider
from dfe_engine.auth.roles import RoleConfig

router = APIRouter(prefix="/auth", tags=["Auth"])


# ── Request / Response models ─────────────────────────────────


class LoginRequest(BaseModel):
    username: str = Field(description="Account name (admin, operator, viewer)")
    password: str = Field(description="Plaintext password")


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT Bearer token")
    token_type: str = Field(default="bearer")
    expires_in: int = Field(description="Token lifetime in seconds")
    user_id: str = Field(description="Authenticated user ID")
    roles: list[str] = Field(description="User roles")


class UserResponse(BaseModel):
    org_id: str
    user_id: str
    roles: list[str]
    permissions: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)


class PermissionsResponse(BaseModel):
    roles: list[str] = Field(description="User's assigned roles")
    permissions: list[str] = Field(description="Resolved permissions from all roles")


# ── Endpoints ────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, settings: Settings):
    """Authenticate with local credentials and receive a JWT token."""
    provider: LocalAuthProvider = request.app.state.auth_provider

    auth_ctx = provider.authenticate(
        body.username,
        body.password,
        request_id=request.headers.get("X-Request-ID"),
        client_ip=_get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )

    token = create_access_token(
        data={
            "sub": auth_ctx.user_id,
            "org_id": auth_ctx.org_id,
            "roles": auth_ctx.roles,
        },
        settings=settings,
    )

    return TokenResponse(
        access_token=token,
        expires_in=settings.api.jwt_expire_minutes * 60,
        user_id=auth_ctx.user_id,
        roles=auth_ctx.roles,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(user: CurrentUser, settings: Settings):
    """Refresh the current JWT token. Requires a valid existing token."""
    token = create_access_token(
        data={
            "sub": user.user_id,
            "org_id": user.org_id,
            "roles": user.roles,
        },
        settings=settings,
    )

    return TokenResponse(
        access_token=token,
        expires_in=settings.api.jwt_expire_minutes * 60,
        user_id=user.user_id,
        roles=user.roles,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: CurrentUser, request: Request):
    """Get the current authenticated user's info."""
    role_config = getattr(request.app.state, "role_config", None) or RoleConfig.load_builtin()
    permissions = sorted(role_config.resolve_permissions(user.roles))
    return UserResponse(
        org_id=user.org_id,
        user_id=user.user_id,
        roles=user.roles,
        permissions=permissions,
        groups=user.groups,
    )


@router.get("/permissions", response_model=PermissionsResponse)
async def get_permissions(user: CurrentUser, request: Request):
    """Get resolved permissions for the current user's roles."""
    role_config = getattr(request.app.state, "role_config", None) or RoleConfig.load_builtin()
    all_perms = role_config.resolve_permissions(user.roles)

    return PermissionsResponse(
        roles=user.roles,
        permissions=sorted(all_perms),
    )


# ── Helpers ──────────────────────────────────────────────────


def _get_client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
