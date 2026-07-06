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
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/accounts", tags=["Accounts"])


async def _revoke_hyperdx_membership_everywhere(request: Request, email: str) -> None:
    """Revoke a user's HyperDX team membership across every org (non-fatal).

    The account -> HyperDX propagation seam: an account being disabled/deleted or
    fully removed from its groups must stop the org's HyperDX admitting them.
    No-op when HyperDX/orgs are not wired (app.state.org_lifecycle absent) or the
    account has no recorded email (local, non-OIDC accounts). Keyed by email
    because HyperDX membership is by email, not the sanitised account username.
    """
    lifecycle = getattr(request.app.state, "org_lifecycle", None)
    if lifecycle is not None and email:
        await lifecycle.revoke_member_everywhere(email)


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
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def create_account(
    body: CreateAccountRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new local user account (admin only)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    if store.get(body.username) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Account '{body.username}' already exists"},
        )
    account = store.create(body.username, body.password, groups=body.groups)
    sync_group_members_for_account_groups_change(
        group_store,
        body.username,
        added=body.groups,
    )
    audit_resource_change(user.user_id, "account", account.username, "created")
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
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
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
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
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
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def update_account(
    username: str,
    body: UpdateAccountRequest,
    user: CurrentUser,
    request: Request,
):
    """Update account groups or enabled status (admin only)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = store.get(username)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    update_fields: dict[str, object] = {}
    if body.groups is not None:
        update_fields["groups"] = body.groups
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.groups is not None:
        old_groups = set(existing.groups)
        new_groups = set(body.groups)
        account = store.update(username, **update_fields)
        sync_group_members_for_account_groups_change(
            group_store,
            username,
            added=new_groups - old_groups,
            removed=old_groups - new_groups,
        )
    else:
        account = store.update(username, **update_fields)
    audit_resource_change(user.user_id, "account", account.username, "updated")
    # Disabling an account revokes its HyperDX team membership on every org
    # (non-fatal). Re-enabling does not re-invite here - login re-provisions.
    if body.enabled is False:
        await _revoke_hyperdx_membership_everywhere(request, account.email)
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
    dependencies=[
        Depends(require_action(scopes_dict["account_reset_password"])),
    ],
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
    audit_resource_change(user.user_id, "account", username, "password_reset")
    return {"message": "password reset"}


@router.delete(
    "/{username}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def delete_account(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Delete an account (admin only)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = store.get(username)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    store.delete(username)
    audit_resource_change(user.user_id, "account", username, "deleted")
    # Create/update sync membership both ways; delete must too, or the username
    # is left dangling in every Group.members list it belonged to.
    sync_group_members_for_account_groups_change(
        group_store,
        username,
        removed=existing.groups,
    )
    # A deleted account loses all access -> revoke its HyperDX team membership on
    # every org (non-fatal, no-op when HyperDX unwired or no email on record).
    await _revoke_hyperdx_membership_everywhere(request, existing.email)
