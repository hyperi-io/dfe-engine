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

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, Settings, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth import account_durability
from dfe_engine.auth.account_durability import AccountGitState
from dfe_engine.auth.accounts import Account
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/accounts", tags=["Accounts"])


def _persist_account(
    request: Request,
    settings: Settings,
    *,
    username: str,
    account: Account,
    summary: str,
    actor: str,
) -> AccountGitState:
    """Mirror an account write into the durable deploy repo and report the state.

    Only the break-glass admin is git-persisted; regular users are durable in their
    own store (document store/yaml), so they never touch the deploy repo.
    """
    if not account_durability.is_break_glass(username, settings.auth.local.admin_name):
        return account_durability.not_git_backed_state()
    gc = getattr(request.app.state, "gitcrud", None)
    forge = getattr(request.app.state, "forge", None)
    outcome = account_durability.publish_account(
        gc,
        forge,
        environment=settings.env,
        mode=settings.gitops.mode,
        username=username,
        doc=account.model_dump(exclude={"username"}),
        summary=summary,
        actor=actor,
        request_id=request.headers.get("X-Request-ID", ""),
    )
    return account_durability.state_from_outcome(gc, outcome)


# Reuse depth quoted in the rejection message; only the current password is
# compared, since no password history is stored. The message stays vague so it
# cannot confirm that a candidate password is the account's current one.
PASSWORD_REUSE_WINDOW = 5


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


class ResetPasswordResponse(BaseModel):
    """Reset outcome plus where the change stands in the durable deploy repo."""

    message: str = Field(default="password reset")
    git: AccountGitState = Field(
        description="Durability state: merged straight away, or a pending PR/command."
    )


class AccountResponse(BaseModel):
    """Account detail — password_hash is NEVER included."""

    username: str
    enabled: bool
    groups: list[str]
    created_at: str
    updated_at: str


class AttributesRequest(BaseModel):
    """Full-replace body for an account's attribute blob (non-sensitive or sensitive)."""

    attributes: dict[str, Any] = Field(default_factory=dict)


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
    settings: Settings,
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
    _persist_account(
        request,
        settings,
        username=account.username,
        account=account,
        summary="create account",
        actor=user.user_id,
    )
    return AccountResponse(
        username=account.username,
        enabled=account.enabled,
        groups=account.groups,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


@router.get(
    "",
    response_model=PaginatedResponse[AccountResponse],
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
)
async def list_accounts(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in username"),
    sort_by: str | None = Query(None, description="Sort field (username, created_at, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List accounts (admin only), paginated. No password hashes returned."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    rows = [
        AccountResponse(
            username=a.username,
            enabled=a.enabled,
            groups=a.groups,
            created_at=a.created_at,
            updated_at=a.updated_at,
        ).model_dump()
        for a in store.list()
    ]
    rows = apply_search(rows, search, ["username"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [AccountResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


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
    settings: Settings,
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
    _persist_account(
        request,
        settings,
        username=account.username,
        account=account,
        summary="update account",
        actor=user.user_id,
    )
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
    response_model=ResetPasswordResponse,
    dependencies=[
        Depends(require_action(scopes_dict["accounts_reset_password"])),
    ],
)
async def reset_password(
    username: str,
    body: ResetPasswordRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> ResetPasswordResponse:
    """Reset an account's password (admin only).

    The live store takes the new password immediately (next login), and the change
    is mirrored into the durable deploy repo so it survives a rebuild. The ``git``
    block reports whether that mirror merged straight away (dev/solo) or is a
    pending review PR / CLI merge (production+team), or is a no-op file share.
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    if store.verify_password(username, body.new_password):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "password_reused",
                "message": (
                    f"New password may not match any of the last {PASSWORD_REUSE_WINDOW} passwords"
                ),
            },
        )
    store.reset_password(username, body.new_password)
    git = _persist_account(
        request,
        settings,
        username=username,
        account=store.get(username),
        summary="reset password",
        actor=user.user_id,
    )
    return ResetPasswordResponse(git=git)


@router.get(
    "/{username}/git-status",
    response_model=AccountGitState,
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
)
async def account_git_status(
    username: str,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> AccountGitState:
    """Poll whether an account's password has merged to the deploy repo's main.

    Step 2 of the review-PR path for the break-glass admin: the UI polls this after
    the operator merges the PR; it fetches remote main and flips ``merged`` true.
    Durable immediately in the auto-merge and file-share modes. A regular user is
    never git-persisted, so it reports not-git-backed (durable in its own store).
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    if not account_durability.is_break_glass(username, settings.auth.local.admin_name):
        return account_durability.not_git_backed_state()
    return account_durability.remote_state(
        getattr(request.app.state, "gitcrud", None),
        store,
        username,
        environment=settings.env,
        mode=settings.gitops.mode,
    )


@router.delete(
    "/{username}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def delete_account(
    username: str,
    user: CurrentUser,
    request: Request,
    settings: Settings,
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
    if account_durability.is_break_glass(username, settings.auth.local.admin_name):
        account_durability.remove_account(
            getattr(request.app.state, "gitcrud", None),
            getattr(request.app.state, "forge", None),
            environment=settings.env,
            mode=settings.gitops.mode,
            username=username,
            actor=user.user_id,
            request_id=request.headers.get("X-Request-ID", ""),
        )


# ── Attributes ───────────────────────────────────────────────
#
# Non-sensitive attributes ride inline on the Account model, so they are read
# and written through the account store. Sensitive attributes live in a separate
# keyed store (never inline, so a broad account read cannot leak them); both
# sensitive endpoints still require the account to exist first.


@router.get(
    "/{username}/attributes",
    dependencies=[Depends(require_action(scopes_dict["account_attributes_read"]))],
)
async def get_account_attributes(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Read an account's non-sensitive attribute blob (404 if the account is missing)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    account = store.get(username)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    return {"attributes": account.attributes}


@router.put(
    "/{username}/attributes",
    dependencies=[Depends(require_action(scopes_dict["account_attributes_write"]))],
)
async def put_account_attributes(
    username: str,
    body: AttributesRequest,
    user: CurrentUser,
    request: Request,
):
    """Full-replace an account's non-sensitive attribute blob (404 if missing)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    try:
        account = store.set_attributes(username, body.attributes)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        ) from exc
    return {"attributes": account.attributes}


@router.get(
    "/{username}/sensitive-attributes",
    dependencies=[Depends(require_action(scopes_dict["account_attributes_read_sensitive"]))],
)
async def get_account_sensitive_attributes(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Read an account's SENSITIVE attribute blob from the separate keyed store.

    The account must exist first (404 otherwise), so a sensitive read cannot be
    used to probe for accounts that are not there.
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    return {"attributes": request.app.state.account_sensitive_attributes.get(username)}


@router.put(
    "/{username}/sensitive-attributes",
    dependencies=[Depends(require_action(scopes_dict["account_attributes_write_sensitive"]))],
)
async def put_account_sensitive_attributes(
    username: str,
    body: AttributesRequest,
    user: CurrentUser,
    request: Request,
):
    """Full-replace an account's SENSITIVE attribute blob (404 if the account is missing)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    if store.get(username) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    request.app.state.account_sensitive_attributes.put(username, body.attributes)
    return {"attributes": request.app.state.account_sensitive_attributes.get(username)}
