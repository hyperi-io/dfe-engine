"""Auth router — login, token refresh, user info, permissions.

POST /api/v1/auth/login           → JWT token (LocalAuthProvider)
POST /api/v1/auth/refresh         → Refreshed JWT token
GET  /api/v1/auth/me              → Current user info
GET  /api/v1/auth/permissions     → Resolved permissions for current user's roles
GET  /api/v1/auth/setup-status    → Initial setup required? (public, pre-login)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    _get_client_ip,
    create_access_token,
    get_role_config,
    require_local_account_enabled,
    resolve_live_groups_for_user,
    resolve_live_roles_for_user,
)
from dfe_engine.auth.audit import audit_login_success
from dfe_engine.auth.local_provider import LocalAuthProvider
from dfe_engine.auth.setup_status import SetupStatus, evaluate_initial_setup

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
    org_ids: list[str] = Field(
        default_factory=list,
        description="Orgs this user can browse in the data plane (HyperDX). Empty "
        "means no org-scoped data access.",
    )


class PermissionsResponse(BaseModel):
    roles: list[str] = Field(description="User's assigned roles")
    permissions: list[str] = Field(description="Resolved permissions from all roles")


# ── Endpoints ────────────────────────────────────────────────


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, settings: Settings):
    """Authenticate with local credentials and receive a JWT token."""
    provider: LocalAuthProvider = request.app.state.auth_provider
    client_ip = _get_client_ip(request)

    auth_ctx = provider.authenticate(
        body.username,
        body.password,
        request_id=request.headers.get("X-Request-ID"),
        client_ip=client_ip,
        user_agent=request.headers.get("User-Agent"),
    )

    # The password is exchanged for a token here, so this is the login the
    # audit trail counts - not the per-request token check in get_current_user.
    audit_login_success(auth_ctx.user_id, "jwt", client_ip, auth_ctx.roles)

    token = create_access_token(
        data={
            "sub": auth_ctx.user_id,
            "org_id": auth_ctx.org_id,
            "roles": auth_ctx.roles,
            "groups": auth_ctx.groups,
            "org_ids": auth_ctx.org_ids,
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
async def refresh_token(user: CurrentUser, request: Request, settings: Settings):
    """Refresh the current JWT token. Requires a valid existing token."""
    require_local_account_enabled(request, user.user_id)
    roles = resolve_live_roles_for_user(request, user.user_id, fallback_groups=user.groups)
    groups = resolve_live_groups_for_user(request, user.user_id, fallback_groups=user.groups)
    token = create_access_token(
        data={
            "sub": user.user_id,
            "org_id": user.org_id,
            "roles": roles,
            "groups": groups,
            "org_ids": user.org_ids,
        },
        settings=settings,
    )

    return TokenResponse(
        access_token=token,
        expires_in=settings.api.jwt_expire_minutes * 60,
        user_id=user.user_id,
        roles=roles,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: CurrentUser, request: Request):
    """Get the current authenticated user's info."""
    role_config = get_role_config(request)
    permissions = sorted(role_config.resolve_permissions(user.roles))
    return UserResponse(
        org_id=user.org_id,
        user_id=user.user_id,
        roles=user.roles,
        permissions=permissions,
        groups=user.groups,
        org_ids=user.org_ids,
    )


@router.get("/permissions", response_model=PermissionsResponse)
async def get_permissions(user: CurrentUser, request: Request):
    """Get resolved permissions for the current user's roles."""
    role_config = get_role_config(request)
    all_perms = role_config.resolve_permissions(user.roles)

    return PermissionsResponse(
        roles=user.roles,
        permissions=sorted(all_perms),
    )


@router.get("/setup-status", response_model=SetupStatus)
async def get_setup_status(request: Request) -> SetupStatus:
    """Report first-run setup state and what is configured (no auth — pre-login UI).

    Driven by the setup state machine (``state_machines/setup.py``). The
    registries the wizard renders — OIDC providers and organisations — are
    returned while setup is outstanding and dropped once it completes, so a
    configured deployment does not serve its inventory to anonymous callers.
    The exception is the name and display name of each enabled OIDC provider,
    which stay (no configuration) because the login screen needs to know which
    IdPs to offer and what to call them. Accounts are never returned; the
    ``first_user`` step reports whether a real (non break-glass) user exists.
    """
    return evaluate_initial_setup(request)
