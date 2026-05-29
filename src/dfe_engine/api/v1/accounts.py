#  Project:      dfe-engine
#  File:         api/v1/accounts.py
#  Purpose:      Account CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Account management router — CRUD for local user accounts.

POST   /api/v1/auth/accounts                        → Create account
GET    /api/v1/auth/accounts                        → List accounts
GET    /api/v1/auth/accounts/{username}             → Get account detail
PUT    /api/v1/auth/accounts/{username}             → Update account
POST   /api/v1/auth/accounts/{username}/reset-password → Reset password
DELETE /api/v1/auth/accounts/{username}             → Delete account

All endpoints require admin role (org:write).
Password hashes are NEVER returned in any response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action

router = APIRouter(prefix="/accounts", tags=["Accounts"])


# ── Request / Response models ────────────────────────────────


class CreateAccountRequest(BaseModel):
    username: str = Field(description="Unique account name")
    password: str = Field(description="Plaintext password (bcrypt-hashed before storage)")
    groups: list[str] = Field(default_factory=list, description="Group memberships")


class UpdateAccountRequest(BaseModel):
    groups: list[str] | None = Field(None, description="Replace group memberships")
    enabled: bool | None = Field(None, description="Enable or disable the account")


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(description="New plaintext password")


class AccountResponse(BaseModel):
    """Account detail — password_hash is NEVER included."""

    username: str
    enabled: bool
    groups: list[str]
    created_at: str
    updated_at: str


# ── Endpoints ────────────────────────────────────────────────


@router.post(
    "",
    response_model=AccountResponse,
    status_code=201,
    dependencies=[Depends(require_action("org:write"))],
)
async def create_account(
    body: CreateAccountRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new local user account (admin only)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(body.username) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Account '{body.username}' already exists"},
        )
    account = store.create(body.username, body.password, groups=body.groups)
    return AccountResponse(
        username=account.username,
        enabled=account.enabled,
        groups=account.groups,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.get(
    "",
    response_model=list[AccountResponse],
    dependencies=[Depends(require_action("org:write"))],
)
async def list_accounts(
    user: CurrentUser,
    request: Request,
):
    """List all accounts (admin only). No password hashes returned."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    return [
        AccountResponse(
            username=a.username,
            enabled=a.enabled,
            groups=a.groups,
            created_at=a.created_at,
            updated_at=a.updated_at,
        )
        for a in store.list()
    ]


@router.get(
    "/{username}",
    response_model=AccountResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def get_account(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single account by username (admin only)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    account = store.get(username)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    return AccountResponse(
        username=account.username,
        enabled=account.enabled,
        groups=account.groups,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.put(
    "/{username}",
    response_model=AccountResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def update_account(
    username: str,
    body: UpdateAccountRequest,
    user: CurrentUser,
    request: Request,
):
    """Update account groups or enabled status (admin only)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    update_fields: dict[str, object] = {}
    if body.groups is not None:
        update_fields["groups"] = body.groups
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    account = store.update(username, **update_fields)
    return AccountResponse(
        username=account.username,
        enabled=account.enabled,
        groups=account.groups,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.post(
    "/{username}/reset-password",
    status_code=200,
    dependencies=[Depends(require_action("org:write"))],
)
async def reset_password(
    username: str,
    body: ResetPasswordRequest,
    user: CurrentUser,
    request: Request,
):
    """Reset an account's password (admin only)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    store.reset_password(username, body.new_password)
    return {"message": "password reset"}


@router.delete(
    "/{username}",
    status_code=204,
    dependencies=[Depends(require_action("org:write"))],
)
async def delete_account(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Delete an account (admin only)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    store.delete(username)
