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
GET    /api/v1/auth/accounts/me                     → Get own account (session)
PUT    /api/v1/auth/accounts/me                     → Update own contact fields (session)
PUT    /api/v1/auth/accounts/{username}             → Update account (admin)
POST   /api/v1/auth/accounts/reset-password         → Reset own password (session)
POST   /api/v1/auth/accounts/{username}/reset-password → Reset password (admin)
DELETE /api/v1/auth/accounts/{username}             → Delete account

Admin endpoints require account write/reset scopes. ``GET/PUT /me`` and
``POST /reset-password`` are the session-owner's own account and only require
a logged-in user. Password hashes are NEVER returned in any response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

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
from dfe_engine.auth.audit import audit_account_change
from dfe_engine.auth.bootstrap import (
    MIN_ADMIN_PASSWORD_LENGTH,
    admin_account_name,
    default_credentials_in_use,
)
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
    """What a console sends to create an account.

    Only the credentials are required. The setup wizard creates the first user
    from a form that collects no contact details, so a required field here is a
    deployment that cannot get into its own console.
    """

    username: str = Field(description="Unique account name")
    password: str = Field(description="Plaintext password (bcrypt-hashed before storage)")
    email: str = Field(default="", description="Contact email")
    groups: list[str] = Field(default_factory=list, description="Group memberships")
    phone: str = Field(default="", description="Contact phone")
    name: str = Field(default="", description="Display name")


class UpdateAccountRequest(BaseModel):
    groups: list[str] | None = Field(None, description="Replace group memberships")
    enabled: bool | None = Field(None, description="Enable or disable the account")
    blocked: bool | None = Field(None, description="Block the account from holding a session")
    email: str | None = Field(None, min_length=1, description="Contact email")
    phone: str | None = Field(None, description="Contact phone")
    name: str | None = Field(None, description="Display name")


class UpdateOwnAccountRequest(BaseModel):
    """Contact fields a session owner may change on their own account."""

    email: str | None = Field(None, min_length=1, description="Contact email")
    phone: str | None = Field(None, description="Contact phone")
    name: str | None = Field(None, description="Display name")


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(description="New plaintext password")


class ResetPasswordResponse(BaseModel):
    """Reset outcome plus where the change stands in the durable deploy repo."""

    message: str = Field(default="password reset")
    git: AccountGitState = Field(
        description="Durability state: merged straight away, or a pending PR/command."
    )


class RotatePasswordRequest(BaseModel):
    """A rotation the deployment can boot on.

    The rotated value becomes ``DFE_AUTH_LOCAL_ADMIN_PASSWORD``, and the boot gate
    refuses an empty or default one outside a dev posture, so an unconstrained
    rotation is a way for an admin to lock the deployment out of its own restart.
    Both rules are the ones that gate startup, not a second opinion on them.
    """

    new_password: str = Field(
        min_length=MIN_ADMIN_PASSWORD_LENGTH,
        description=(
            "New plaintext password to write to the store; at least "
            f"{MIN_ADMIN_PASSWORD_LENGTH} characters and never the shipped default"
        ),
    )

    @field_validator("new_password")
    @classmethod
    def _refuse_default_credentials(cls, value: str) -> str:
        if default_credentials_in_use(value):
            raise ValueError(
                "the shipped default admin password -- the engine refuses to start "
                "on it outside a dev posture; choose another"
            )
        return value


class RotatePasswordResponse(BaseModel):
    """Outcome of a rotation written through the secrets seam."""

    message: str = Field(default="password rotated in the secret store")
    secret_path: str = Field(description="scalo.secrets path the new password was written to")


class AccountResponse(BaseModel):
    """Account detail — password_hash is NEVER included."""

    username: str
    enabled: bool
    blocked: bool
    disabled_at: str = ""
    blocked_at: str = ""
    groups: list[str]
    email: str
    phone: str = ""
    name: str = ""
    external: bool = Field(
        description="True when the account authenticates through an identity provider",
    )
    created_at: str
    updated_at: str


class AttributesRequest(BaseModel):
    """Full-replace body for an account's attribute blob (non-sensitive or sensitive)."""

    attributes: dict[str, Any] = Field(default_factory=dict)


def _account_response(account: Account) -> AccountResponse:
    """Map a stored account to the public response (never includes password_hash)."""
    return AccountResponse(
        username=account.username,
        enabled=account.enabled,
        blocked=account.blocked,
        disabled_at=account.disabled_at,
        blocked_at=account.blocked_at,
        groups=account.groups,
        email=account.email,
        phone=account.phone,
        name=account.name,
        external=account.external,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _contact_updates(
    body: UpdateAccountRequest | UpdateOwnAccountRequest,
) -> dict[str, object]:
    """Partial contact-field updates; omitted values are left unchanged."""
    fields: dict[str, object] = {}
    if body.email is not None:
        fields["email"] = body.email
    if body.phone is not None:
        fields["phone"] = body.phone
    if body.name is not None:
        fields["name"] = body.name
    return fields


def _require_account(store: Any, username: str) -> Account:
    account = store.get(username)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    return account


def _reset_stored_password(
    request: Request,
    settings: Settings,
    *,
    username: str,
    new_password: str,
    actor: str,
) -> ResetPasswordResponse:
    """Apply a password reset in the live store and mirror it if git-backed."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    existing = store.get(username)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Account '{username}' not found"},
        )
    if existing.external:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "external_account",
                "message": (
                    f"Account '{username}' authenticates through its identity "
                    "provider; a local password cannot be set"
                ),
            },
        )
    if store.verify_password(username, new_password):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "password_reused",
                "message": (
                    f"New password may not match any of the last {PASSWORD_REUSE_WINDOW} passwords"
                ),
            },
        )
    store.reset_password(username, new_password)
    git = _persist_account(
        request,
        settings,
        username=username,
        account=store.get(username) or existing,
        summary="reset password",
        actor=actor,
    )
    return ResetPasswordResponse(git=git)


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
    account = store.create(
        body.username,
        body.password,
        groups=body.groups,
        email=body.email,
        phone=body.phone,
        name=body.name,
    )
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
    return _account_response(account)


@router.get(
    "",
    response_model=PaginatedResponse[AccountResponse],
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
)
async def list_accounts(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in username, name, or email"),
    blocked: bool | None = Query(
        None,
        description="Filter by blocked status. Omitted returns every account.",
    ),
    include_core: bool = Query(
        False,
        description="Include the local admin and break-glass recovery accounts.",
    ),
    sort_by: str | None = Query(None, description="Sort field (username, created_at, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List accounts (admin only), paginated. No password hashes returned."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    accounts = store.list()
    if not include_core:
        accounts = [
            account for account in accounts if not store.protected.is_protected(account.username)
        ]
    if blocked is not None:
        accounts = [account for account in accounts if account.blocked is blocked]
    rows = [_account_response(a).model_dump() for a in accounts]
    rows = apply_search(rows, search, ["username", "name", "email"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [AccountResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get("/me", response_model=AccountResponse)
async def get_current_user_account(
    user: CurrentUser,
    request: Request,
):
    """Return the authenticated user's account. No extra scope required."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    return _account_response(_require_account(store, user.user_id))


@router.put("/me", response_model=AccountResponse)
async def update_current_user_account(
    body: UpdateOwnAccountRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
):
    """Update the authenticated user's contact fields. No extra scope required.

    Groups and enabled cannot be changed here: those stay on the admin
    ``PUT /{username}`` route, which requires ``account:write``.
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    existing = _require_account(store, user.user_id)
    fields = _contact_updates(body)
    account = store.update(user.user_id, **fields) if fields else existing
    _persist_account(
        request,
        settings,
        username=account.username,
        account=account,
        summary="update own account",
        actor=user.user_id,
    )
    return _account_response(account)


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
    return _account_response(account)


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
    """Update account groups, enabled/blocked status, or contact fields (admin only)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = _require_account(store, username)
    update_fields: dict[str, object] = _contact_updates(body)
    if body.groups is not None:
        update_fields["groups"] = body.groups
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.blocked is not None:
        update_fields["blocked"] = body.blocked
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
    return _account_response(account)


@router.post(
    "/reset-password",
    status_code=200,
    response_model=ResetPasswordResponse,
)
async def reset_current_user_password(
    body: ResetPasswordRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> ResetPasswordResponse:
    """Reset the authenticated user's password.

    The username is taken from the session, not the request, so a caller cannot
    reset another account through this route. An IdP-owned (``external``)
    account is refused: it has no local password. The live store takes the new
    password immediately; the ``git`` block reports whether the durable mirror
    merged, is pending review, or is a no-op for a non-git-backed account.
    """
    return _reset_stored_password(
        request,
        settings,
        username=user.user_id,
        new_password=body.new_password,
        actor=user.user_id,
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

    An IdP-owned (``external``) account is refused: the password lives at the
    identity provider. The live store takes the new password immediately (next
    login), and the change is mirrored into the durable deploy repo so it
    survives a rebuild. The ``git`` block reports whether that mirror merged
    straight away (dev/solo) or is a pending review PR / CLI merge
    (production+team), or is a no-op file share.
    """
    return _reset_stored_password(
        request,
        settings,
        username=username,
        new_password=body.new_password,
        actor=user.user_id,
    )


@router.post(
    "/{username}/rotate-password",
    status_code=200,
    response_model=RotatePasswordResponse,
    dependencies=[
        Depends(require_action(scopes_dict["accounts_reset_password"])),
    ],
)
async def rotate_password(
    username: str,
    body: RotatePasswordRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> RotatePasswordResponse:
    """Rotate an account's password in the deployment's secret store.

    The store that injects ``DFE_AUTH_LOCAL_ADMIN_PASSWORD`` is the source of that
    password, so the engine writes the new value through the scalo secrets seam
    and never into its own YAML -- a YAML-only change is reverted by the next boot
    reconcile. Returns 501 with the store command when the deployment has not
    declared a secrets path for the password.
    """
    from dfe_engine.auth.deployment_hints import detect_deploy_kind, rotation_store_command
    from dfe_engine.secrets import build_secrets

    local = settings.auth.local
    if username != admin_account_name(local.admin_name):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "not_store_backed",
                "message": (
                    f"Only the local admin password is held in the secret store; "
                    f"use reset-password for '{username}'"
                ),
            },
        )
    if not local.admin_password_secret_path:
        kind = detect_deploy_kind(settings.deployment.target)
        raise HTTPException(
            status_code=501,
            detail={
                "code": "secrets_seam_not_wired",
                "message": (
                    "This deployment does not source the admin password from the "
                    "secrets seam, so rotation is a store operation"
                ),
                "store_command": rotation_store_command(
                    kind,
                    namespace=settings.deployment.namespace,
                    secret_name=local.admin_secret_name,
                    secret_key=local.admin_secret_key,
                ),
            },
        )

    build_secrets(settings.secrets).put(local.admin_password_secret_path, body.new_password)
    audit_account_change(user.user_id, username, "rotated password in the secret store")
    return RotatePasswordResponse(secret_path=local.admin_password_secret_path)


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
