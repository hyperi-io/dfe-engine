#  Project:      dfe-engine
#  File:         api/v1/accounts.py
#  Purpose:      Account CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Account management router -- CRUD for local user accounts.

POST   /api/v1/auth/accounts                        -> Create account
GET    /api/v1/auth/accounts                        -> List accounts
GET    /api/v1/auth/accounts/{username}             -> Get account detail
GET    /api/v1/auth/accounts/me                     -> Get own account (session)
PUT    /api/v1/auth/accounts/me                     -> Update own contact fields (session)
PUT    /api/v1/auth/accounts/{username}             -> Update account (admin)
POST   /api/v1/auth/accounts/reset-password         -> Change own password (session + current)
POST   /api/v1/auth/accounts/{username}/reset-password -> Reset password (admin)
DELETE /api/v1/auth/accounts/{username}             -> Delete account

Admin endpoints require account write/reset scopes. ``GET/PUT /me`` and
``POST /reset-password`` are the session-owner's own account and only require
a logged-in user. Password hashes are NEVER returned in any response.
"""

import functools
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from scalo.api import (
    ListQuery,
    Page,
    ProblemError,
    list_query,
    paginate,
    scalo_extra,
    sort_by_order,
)
from scalo.concurrency import run_blocking

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import (
    CurrentUser,
    ReauthGrant,
    Settings,
    _get_client_ip,
    account_for_session_subject,
    bound_org,
    is_own_account,
    provider_bindings,
    reaches_org,
    reads_every_org,
    refuse_unknown_orgs,
    refuse_unreachable_org,
    refuse_without_grant,
    registered_orgs,
    require_account_reauth,
    require_action,
    require_own_org_action,
    require_platform_action,
    secrets_store,
    throttled,
)
from dfe_engine.api.v1.account_groups import check_group_changes, refuse_org_role_breach
from dfe_engine.api.v1.auth import persist_session_end
from dfe_engine.auth import account_durability, breakglass
from dfe_engine.auth.account_durability import AccountGitState
from dfe_engine.auth.accounts import (
    BCRYPT_MAX_PASSWORD_BYTES,
    Account,
    AccountExistsError,
    delete_link_records,
    matches_digest,
    refuse_unhashable_password,
)
from dfe_engine.auth.admin_retirement import account_holds_system_admin
from dfe_engine.auth.attributes import check_attribute_depth
from dfe_engine.auth.audit import (
    audit_account_change,
    audit_identity_unlinked,
    audit_login_denied,
)
from dfe_engine.auth.bootstrap import (
    MIN_ADMIN_PASSWORD_LENGTH,
    admin_account_name,
    default_credentials_in_use,
)
from dfe_engine.auth.groups import Group
from dfe_engine.auth.identity_links import link_candidates, linked_identity, without_identity
from dfe_engine.auth.login_throttle import LoginThrottle
from dfe_engine.auth.membership import (
    forget_member,
    groups_held,
    groups_held_after,
    groups_held_by_link,
)
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.names import VALID_NAME, InvalidNameError, StoreName
from dfe_engine.orgs.role_groups import usable_org_admins

router = APIRouter(prefix="/accounts", tags=["Accounts"])


async def _persist_account(
    request: Request,
    settings: Settings,
    *,
    username: str,
    account: Account,
    summary: str,
    actor: str,
) -> AccountGitState:
    """Mirror an account write into the durable deploy repo and report the state.

    Only the local admin's account doc is git-persisted. Regular users are durable
    in their own YAML store, so they never touch the deploy repo.
    The mirror is a commit and a push, so it runs on a worker thread.
    """
    if not account_durability.is_local_admin(username, settings.auth.local.admin_name):
        return account_durability.not_git_backed_state()
    return await run_blocking(
        functools.partial(
            _mirror_account,
            request,
            settings,
            username=username,
            account=account,
            summary=summary,
            actor=actor,
        )
    )


def _mirror_account(
    request: Request,
    settings: Settings,
    *,
    username: str,
    account: Account,
    summary: str,
    actor: str,
) -> AccountGitState:
    """Commit the local admin's account doc to the deploy repo."""
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


def _publish_breakglass_hash(
    request: Request, settings: Settings, *, digest: str, actor: str
) -> AccountGitState:
    """Commit breakglass's new hash to the governance settings it is seeded from."""
    gc = getattr(request.app.state, "gitcrud", None)
    outcome = account_durability.publish_breakglass_hash(
        gc,
        getattr(request.app.state, "forge", None),
        environment=settings.env,
        mode=settings.gitops.mode,
        digest=digest,
        summary="reset password",
        actor=actor,
        request_id=request.headers.get("X-Request-ID", ""),
    )
    return account_durability.state_from_outcome(gc, outcome)


async def _persist_password(
    request: Request,
    settings: Settings,
    *,
    username: str,
    account: Account,
    actor: str,
) -> AccountGitState:
    """Make a new password durable where that account's password is kept.

    The local admin's whole doc is mirrored into the deploy repo, ``breakglass``
    keeps only its hash in the governance settings, and any other account is
    durable in its own store. Either commit is a git write, so it runs off the loop.
    """
    if username == breakglass.USERNAME:
        return await run_blocking(
            functools.partial(
                _publish_breakglass_hash,
                request,
                settings,
                digest=account.password_hash,
                actor=actor,
            )
        )
    return await _persist_account(
        request,
        settings,
        username=username,
        account=account,
        summary="reset password",
        actor=actor,
    )


# -- Request / Response models --------------------------------

_ORG_DESCRIPTION = (
    "The registered org the account belongs to, which its sessions carry as org_id. "
    "Empty makes it a root account, bound to your org (GET /system/organisation), or "
    "to none while no org is yours. The binding grants nothing itself: an org-bound "
    "view also needs the org among those its groups grant."
)


class CreateAccountRequest(BaseModel):
    """What a console sends to create an account: the credentials and a display name, with the contact fields optional."""

    username: StoreName = Field(description="Unique account name")
    password: str = Field(
        min_length=MIN_ADMIN_PASSWORD_LENGTH,
        description=(
            "Plaintext password (bcrypt-hashed before storage); at least "
            f"{MIN_ADMIN_PASSWORD_LENGTH} characters and at most "
            f"{BCRYPT_MAX_PASSWORD_BYTES} bytes"
        ),
    )
    email: str = Field(default="", description="Contact email")
    groups: list[str] = Field(
        description="Group memberships, at least one: an account holds its roles only through its groups",
        min_length=1,
    )
    phone: str = Field(default="", description="Contact phone")
    name: str = Field(min_length=1, description="Display name")
    org: str = Field(default="", description=_ORG_DESCRIPTION)

    @field_validator("name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not (value.strip()):
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("password")
    @classmethod
    def _within_the_hash_limit(cls, value: str) -> str:
        return refuse_unhashable_password(value)


class UpdateAccountRequest(BaseModel):
    """Body for updating an account, where an omitted field keeps its value."""

    groups: list[str] | None = Field(
        None, description="Replace group memberships, with at least one", min_length=1
    )
    enabled: bool | None = Field(None, description="Enable or disable the account")
    blocked: bool | None = Field(None, description="Block the account from holding a session")
    email: str | None = Field(None, min_length=1, description="Contact email")
    phone: str | None = Field(None, description="Contact phone")
    name: str | None = Field(None, description="Display name")
    org: str | None = Field(None, description=f"Replace the org binding. {_ORG_DESCRIPTION}")


class UpdateOwnAccountRequest(BaseModel):
    """Contact fields a session owner may change on their own account."""

    email: str | None = Field(None, min_length=1, description="Contact email")
    phone: str | None = Field(None, description="Contact phone")
    name: str | None = Field(None, description="Display name")


class ResetPasswordRequest(BaseModel):
    """Body for setting an account's new password."""

    new_password: str = Field(
        min_length=MIN_ADMIN_PASSWORD_LENGTH,
        description=(
            f"New plaintext password; at least {MIN_ADMIN_PASSWORD_LENGTH} characters and "
            f"at most {BCRYPT_MAX_PASSWORD_BYTES} bytes"
        ),
    )

    @field_validator("new_password")
    @classmethod
    def _within_the_hash_limit(cls, value: str) -> str:
        return refuse_unhashable_password(value)


class ChangeOwnPasswordRequest(ResetPasswordRequest):
    """The owner's own change, which proves the password as well as the session."""

    current_password: str = Field(
        min_length=1,
        description="The account's password now. A session token alone cannot change it.",
    )


class ResetPasswordResponse(BaseModel):
    """Reset outcome plus where the change stands in the durable deploy repo."""

    message: str = Field(default="password reset")
    git: AccountGitState = Field(
        description="Durability state: merged straight away, or a pending PR/command."
    )


class RotatePasswordRequest(BaseModel):
    """A rotation the deployment can boot on.

    The rotated value becomes ``DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD``, and the boot gate
    refuses an empty or default one outside a dev posture, so an unconstrained
    rotation is a way for an admin to lock the deployment out of its own restart.
    Both rules are the ones that gate startup, not a second opinion on them.
    """

    new_password: str = Field(
        min_length=MIN_ADMIN_PASSWORD_LENGTH,
        description=(
            "New plaintext password to write to the secret store; at least "
            f"{MIN_ADMIN_PASSWORD_LENGTH} characters, at most {BCRYPT_MAX_PASSWORD_BYTES} "
            "bytes, and never the shipped default"
        ),
    )

    @field_validator("new_password")
    @classmethod
    def _refuse_default_credentials(cls, value: str) -> str:
        # The boot reconcile bcrypt-hashes this value, so a longer one could never be issued.
        refuse_unhashable_password(value)
        if default_credentials_in_use(value):
            raise ValueError(
                "That is the default admin password, which the engine will not start "
                "on outside a development deployment. Choose another."
            )
        return value


class RotatePasswordResponse(BaseModel):
    """Outcome of a rotation written to the deployment's secrets backend."""

    message: str = Field(
        default=(
            "Password saved in the secret store. The engine gives it to the admin at "
            "its next restart, and the admin must change it at the next sign-in."
        )
    )
    secret_path: str = Field(description="Where in the secret store the new password went")


class LinkedIdentityResponse(BaseModel):
    """An IdP sign-in linked to an account, which reaches it without a password."""

    key: str = Field(description="The sign-in's key, which unlinking names")
    provider: str = Field(description="The OIDC provider it signs in through")
    provider_type: str = Field(
        default="",
        description="The provider's type (google, entra_id, okta or generic), empty once it "
        "is no longer registered",
    )
    subject: str = Field(description="The IdP subject (sub)")
    email: str = Field(default="", description="The email its IdP last asserted")
    linked_at: str = ""
    last_login_at: str = ""


class LinkedGroupResponse(BaseModel):
    """A group an account holds only through an IdP sign-in."""

    name: str
    provider: str = Field(description="The provider whose assertion links the group")
    provider_type: str = Field(
        default="",
        description="The provider's type (google, entra_id, okta or generic), empty once it "
        "is no longer registered",
    )


class AccountResponse(BaseModel):
    """Account detail -- password_hash is NEVER included."""

    username: str
    enabled: bool
    blocked: bool
    disabled_at: str = ""
    blocked_at: str = ""
    groups: list[str] = Field(
        description="Names of the groups the account holds: those whose group file lists "
        "it, plus, when an identity provider owns it, the groups the provider's asserted "
        "ids are linked to by source_id.",
    )
    email: str
    phone: str = ""
    name: str = ""
    org: str = Field(default="", description=_ORG_DESCRIPTION)
    org_display_name: str = Field(
        default="",
        description="The display name of the org the account is bound to, which for a root "
        "account is your org, else that org's name when it has none. Empty while it is "
        "bound to none.",
    )
    org_enabled: bool = Field(
        default=True,
        description="False while the account's org is disabled, which refuses its sign-in "
        "whatever the account's own state",
    )
    external: bool = Field(
        description="True when the account authenticates through an identity provider",
    )
    admin: bool = Field(
        default=False,
        description="True when a system-wide group gives the account the admin role, so it "
        "has full access to every org. The account's own read reports false while "
        "password_change_required is set.",
    )
    oidc_id: str = Field(
        default="",
        description="OIDC provider name last used to log this account in, empty if none",
    )
    oidc_type: str = Field(
        default="",
        description="The type of the provider oidc_id names, empty when it names no "
        "registered provider",
    )
    email_verified: bool | None = Field(
        default=None,
        description="Whether the email is vouched for, so a first IdP sign-in with it links "
        "to this account: true when an admin set it or a trusted identity provider verified "
        "it, false when the account owner set it or the provider did not vouch, null when it "
        "was stored before this was recorded",
    )
    identities: list[LinkedIdentityResponse] = Field(
        default_factory=list,
        description="IdP sign-ins linked to this account, each reaching it without a password",
    )
    password_change_required: bool = Field(
        default=False,
        description="True until the account replaces an issued password. The account's "
        "own read reports no groups while it is set, as its session holds none.",
    )
    linked_groups: list[LinkedGroupResponse] = Field(
        default_factory=list,
        description="The groups in groups held only through an IdP sign-in, each with its "
        "provider, so they change with what the IdP asserts rather than with a membership "
        "write",
    )
    created_at: str
    updated_at: str
    last_login_at: str = Field(
        default="",
        description="When the account last signed in, by password or through an identity "
        "provider, empty if it never has",
    )


class AccountAvailabilityResponse(BaseModel):
    """Why creating an account with these values would be refused, empty where it would not."""

    email: str = Field(
        default="", description="Why the email cannot be used, empty when it can or none was given"
    )
    username: str = Field(
        default="",
        description="Why the username cannot be taken, empty when it can or none was given",
    )


class AttributesRequest(BaseModel):
    """Full-replace body for an account's attribute blob (non-sensitive or sensitive)."""

    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def _bounded_depth(cls, value: dict[str, Any]) -> dict[str, Any]:
        return check_attribute_depth(value)


@dataclass(frozen=True, slots=True)
class _Lookups:
    """The registered orgs' display names and OIDC providers' types, each by name, and the disabled orgs."""

    disabled_orgs: frozenset[str]
    org_display_names: Mapping[str, str]
    provider_types: Mapping[str, str]


def _lookups(request: Request) -> _Lookups:
    """The org display names and OIDC provider types as registered now."""
    registry = getattr(request.app.state, "oidc_provider_registry", None)
    providers = registry.list() if registry is not None else []
    orgs = registered_orgs(request)
    return _Lookups(
        disabled_orgs=frozenset(org.name for org in orgs if not (org.enabled)),
        org_display_names={org.name: org.display_name for org in orgs},
        provider_types={name: provider.type for name, provider in providers},
    )


def _account_response(
    *,
    account: Account,
    groups: list[Group],
    lookups: _Lookups,
    request: Request,
) -> AccountResponse:
    """Map a stored account to the public response, which never includes password_hash.

    *groups* and *lookups* are each read once per request.
    """
    bound = bound_org(account_org=account.org, request=request)
    return AccountResponse(
        username=account.username,
        enabled=account.enabled,
        blocked=account.blocked,
        disabled_at=account.disabled_at,
        blocked_at=account.blocked_at,
        groups=groups_held(account, groups, bindings=provider_bindings(request)),
        email=account.email,
        phone=account.phone,
        name=account.name,
        org=account.org,
        org_display_name=(lookups.org_display_names.get(bound)) or (bound),
        org_enabled=account.org not in lookups.disabled_orgs,
        external=account.external,
        admin=account_holds_system_admin(
            account=account, bindings=provider_bindings(request), groups=groups
        ),
        oidc_id=account.oidc_id,
        oidc_type=lookups.provider_types.get(account.oidc_id, ""),
        email_verified=account.email_verified,
        identities=[
            LinkedIdentityResponse.model_validate(
                {
                    **identity.model_dump(exclude={"groups"}),
                    "provider_type": lookups.provider_types.get(identity.provider, ""),
                }
            )
            for identity in account.identities
        ],
        password_change_required=account.password_change_required,
        linked_groups=[
            LinkedGroupResponse(
                name=name,
                provider=provider,
                provider_type=lookups.provider_types.get(provider, ""),
            )
            for name, provider in groups_held_by_link(
                account, groups, bindings=provider_bindings(request)
            ).items()
        ],
        created_at=account.created_at,
        updated_at=account.updated_at,
        last_login_at=account.last_login_at,
    )


def _contact_updates(
    body: UpdateAccountRequest | UpdateOwnAccountRequest, existing: Account, *, vouched: bool
) -> dict[str, object]:
    """Partial contact-field updates. Omitted values are left unchanged, and a new email is vouched for only when an admin sets it."""
    fields: dict[str, object] = {}
    if body.email is not None:
        fields["email"] = body.email
    if (body.email is not None) and (body.email.strip() != existing.email.strip()):
        fields["email_verified"] = vouched
    if body.phone is not None:
        fields["phone"] = body.phone
    if body.name is not None:
        if not (body.name.strip()) and existing.name.strip():
            raise ProblemError(422, "An account's name cannot be cleared", code="name_required")
        fields["name"] = body.name
    return fields


def _require_account(store: Any, username: str) -> Account:
    """Return the account, or 404 when it is missing, soft-deleted or only links a sign-in to another."""
    account = store.get(username)
    if (account is None) or (account.is_soft_deleted) or (account.is_link_record):
        raise ProblemError(404, f"Account '{username}' not found", code="not_found")
    return account


def _require_reachable(*, action: str, request: Request, user: Any, username: str) -> Account:
    """The account *username*, 404 when it is missing or the caller may not take *action* on it, so another org's names stay hidden."""
    account = _require_account(request.app.state.account_store, username)
    if not (reaches_org(action=action, org=account.org, request=request, user=user)):
        raise ProblemError(404, f"Account '{username}' not found", code="not_found")
    return account


def _usable_org_admin(
    *, account: Account | None, groups: list[Any], held: Iterable[str] | None, request: Request
) -> bool:
    """Whether *account* can sign in holding its own org's admins group, an org other than yours."""
    if (account is None) or (not (account.org)) or (account.session_denied() is not None):
        return False
    if account.org == bound_org(account_org="", request=request):
        return False
    if held is None:
        held = groups_held(account=account, bindings=provider_bindings(request), groups=groups)
    return f"{account.org}-admins" in held


def _refuse_last_org_admin(
    *,
    after: Account | None,
    before: Account,
    held_after: Iterable[str] | None,
    request: Request,
    user: Any,
) -> None:
    """Refuse 409 ``last_org_admin`` when a write leaves *before*'s org with no usable admin, unless the caller acts across every org."""
    if reads_every_org(request, user, scopes_dict["account_write"]):
        return
    groups = request.app.state.group_store.list()
    if not (_usable_org_admin(account=before, groups=groups, held=None, request=request)):
        return
    stays = (after is not None) and (after.org == before.org)
    if (stays) and (
        _usable_org_admin(account=after, groups=groups, held=held_after, request=request)
    ):
        return
    admins = usable_org_admins(
        accounts=request.app.state.account_store.scan(),
        bindings=provider_bindings(request),
        groups=groups,
        org=before.org,
    )
    if any(admin != before.username for admin in admins):
        return
    raise ProblemError(
        409,
        f"Cannot leave org {before.org!r} without an admin: {before.username!r} is its last",
        code="last_org_admin",
    )


_EMAIL_TAKEN_MESSAGE = "Another account already uses this email"


def _email_taken(*, email: str, request: Request, username: str) -> bool:
    """Whether an account other than *username* that a sign-in could link to by *email* holds it."""
    store = request.app.state.account_store
    holders = link_candidates(
        accounts=store.scan(), email=email, protected=store.protected.usernames
    )
    return any(holder.username != username for holder in holders)


def _refuse_taken_email(*, email: str, request: Request, username: str) -> None:
    """Refuse *email* when another account a sign-in could link to by it holds it."""
    if _email_taken(email=email, request=request, username=username):
        raise ProblemError(409, _EMAIL_TAKEN_MESSAGE, code="email_taken")


def _require_own_account(store: Any, user_id: str) -> Account:
    """The account the session is bound to, found the way authentication found it.

    A proxied or RP-login subject such as ``jane@corp.com`` is stored under its
    JIT stem, so looking up the raw subject alone misses the caller's own account.
    """
    account = account_for_session_subject(store, user_id)
    if account is None:
        raise ProblemError(404, f"Account '{user_id}' not found", code="not_found")
    return account


def _refuse_external(account: Account) -> None:
    """Refuse a local password for an account its identity provider authenticates."""
    if account.external:
        raise ProblemError(
            409,
            f"'{account.username}' signs in through its identity provider, so "
            "change its password there, not in DFE.",
            code="external_account",
        )


def _write_new_password(store: Any, existing: Account, new_password: str) -> bool:
    """Store *new_password* on *existing*, unless it is the current or last issued one.

    Blocking: three bcrypt operations, so the caller runs it off the event loop.

    Returns:
        False when the password was refused as reused, True once it is stored.
    """
    # No history is stored: the current password and the last one issued are all there is.
    reused = store.verify_password(existing.username, new_password) or matches_digest(
        new_password, existing.seeded_password_hash
    )
    if reused:
        return False
    store.reset_password(existing.username, new_password)
    return True


async def _reset_stored_password(
    request: Request,
    settings: Settings,
    *,
    username: str,
    new_password: str,
    actor: str,
) -> ResetPasswordResponse:
    """Apply a password reset in the live store and commit it where it is kept.

    The new password ends every session the account held (its session marker moves).
    That end goes to the deploy repo's main ahead of the password, which may wait on
    a review, so a restart before the review merges does not revive those sessions.
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    existing = store.get(username)
    if existing is None:
        raise ProblemError(404, f"Account '{username}' not found", code="not_found")
    _refuse_external(existing)
    if not await run_blocking(_write_new_password, store, existing, new_password):
        raise ProblemError(
            400,
            "New password must differ from the account's current password "
            "and from the password it was last issued",
            code="password_reused",
        )
    stored = store.get(username) or existing
    await persist_session_end(request, stored, actor=actor)
    git = await _persist_password(
        request,
        settings,
        username=username,
        account=stored,
        actor=actor,
    )
    return ResetPasswordResponse(git=git)


# -- Endpoints ------------------------------------------------


def _refuse_new_account(*, body: CreateAccountRequest, request: Request, user) -> None:
    """Raise every refusal an account create answers before it writes."""
    refuse_unreachable_org(
        action=scopes_dict["account_write"],
        noun="accounts",
        org=body.org,
        request=request,
        user=user,
    )
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    if store.get(body.username) is not None:
        raise ProblemError(409, f"Account '{body.username}' already exists", code="conflict")
    if body.email.strip():
        _refuse_taken_email(email=body.email, request=request, username=body.username)
    refuse_unknown_orgs(request, [body.org])
    groups = group_store.list()
    created = Account(groups=body.groups, password_hash="", username=body.username)
    held_after = groups_held_after(
        account=created,
        added=body.groups,
        bindings=provider_bindings(request),
        groups=groups,
        removed=(),
    )
    check_group_changes(after=held_after, before=(), groups=groups, request=request, user=user)
    refuse_org_role_breach(
        account_org=body.org,
        groups=[group for group in groups if group.name in set(held_after)],
        request=request,
        username=body.username,
    )


async def refuse_new_account(
    body: CreateAccountRequest, request: Request, user: CurrentUser
) -> None:
    """FastAPI dependency: answer a create's own refusals before its re-auth grant is asked for."""
    _refuse_new_account(body=body, request=request, user=user)


async def require_new_account_reauth(
    body: CreateAccountRequest, request: Request, user: CurrentUser, reauth: ReauthGrant = ""
) -> None:
    """FastAPI dependency: the caller's re-auth grant for the account the body creates."""
    refuse_without_grant(reauth=reauth, request=request, target=body.username, user=user)


@router.post(
    "",
    response_model=AccountResponse,
    status_code=201,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["account_write"])),
        Depends(refuse_new_account),
        Depends(require_new_account_reauth),
    ],
)
async def create_account(
    body: CreateAccountRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
):
    """Create a new local user account: an admin of an org other than yours creates it in that org only."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    conflict = ProblemError(409, f"Account '{body.username}' already exists", code="conflict")
    # The create decides: another replica can take the name after the lookup above.
    # It hashes the password, so it runs off the event loop.
    try:
        account = await run_blocking(
            functools.partial(
                store.create,
                body.username,
                body.password,
                groups=body.groups,
                email=body.email,
                email_verified=True if body.email.strip() else None,
                phone=body.phone,
                name=body.name,
                org=body.org,
            )
        )
    except AccountExistsError as exc:
        raise conflict from exc
    except InvalidNameError as exc:
        raise ProblemError(422, str(exc), code="validation_error") from exc
    sync_group_members_for_account_groups_change(
        group_store,
        body.username,
        added=body.groups,
    )
    await _persist_account(
        request,
        settings,
        username=account.username,
        account=account,
        summary="create account",
        actor=user.user_id,
    )
    audit_account_change(
        user.user_id, account.username, "created", {"groups": body.groups, "org": body.org}
    )
    return _account_response(
        account=account,
        groups=group_store.list(),
        lookups=_lookups(request),
        request=request,
    )


AccountsList = Annotated[
    ListQuery,
    Depends(
        list_query(order_by=("username", "created_at", "updated_at", "org"), tiebreak="username")
    ),
]


@router.get(
    "",
    response_model=Page[AccountResponse],
    dependencies=[Depends(require_own_org_action(scopes_dict["account_read"]))],
)
async def list_accounts(
    user: CurrentUser,
    request: Request,
    query: AccountsList,
    search: str | None = Query(None, description="Search in username, name, or email"),
    oidc_id: str | None = Query(
        None,
        description="Exact match on an OIDC provider the account signs in through: its own, "
        "or one with a sign-in linked to it",
    ),
    org: str | None = Query(
        None,
        description="Exact match on the org the account belongs to, where your org also "
        "matches the root accounts naming none. Empty matches only the root accounts.",
    ),
    blocked: bool | None = Query(
        None,
        description="Filter by blocked status. Omitted returns every account.",
    ),
    include_core: bool = Query(
        False,
        description="Include the local admin and break-glass recovery accounts.",
    ),
):
    """List the accounts the caller administers, paginated: every account across orgs, else its orgs' own. No password hashes returned."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    action = scopes_dict["account_read"]
    accounts = [
        account
        for account in store.list()
        if (not (account.is_soft_deleted))
        and (not (account.is_link_record))
        and (reaches_org(action=action, org=account.org, request=request, user=user))
    ]
    if not include_core:
        accounts = [
            account for account in accounts if not store.protected.is_protected(account.username)
        ]
    if blocked is not None:
        accounts = [account for account in accounts if account.blocked is blocked]
    if oidc_id is not None:
        accounts = [
            account
            for account in accounts
            if (account.oidc_id == oidc_id)
            or (any(identity.provider == oidc_id for identity in account.identities))
        ]
    if org is not None:
        home = bound_org(account_org="", request=request)
        accounts = [
            account for account in accounts if org in (account.org, (account.org) or (home))
        ]
    groups = request.app.state.group_store.list()
    lookups = _lookups(request)
    rows = [
        _account_response(
            account=account, groups=groups, lookups=lookups, request=request
        ).model_dump()
        for account in accounts
    ]
    rows = apply_search(rows, search, ["username", "name", "email"])
    rows = sort_by_order(rows, query.order_by)
    summaries = [AccountResponse.model_validate(row) for row in rows]
    return paginate(
        summaries,
        query,
        bound={
            "search": search,
            "oidc_id": oidc_id,
            "org": org,
            "blocked": blocked,
            "include_core": include_core,
        },
    )


@router.get(
    "/availability",
    response_model=AccountAvailabilityResponse,
    dependencies=[Depends(require_own_org_action(scopes_dict["account_write"]))],
)
async def get_account_availability(
    request: Request,
    email: str = Query("", description="An email a new account would hold"),
    username: str = Query("", description="A name a new account would take"),
):
    """Whether a new account could take *username* and hold *email*, judged as account create judges them."""
    store = request.app.state.account_store
    response = AccountAvailabilityResponse()
    if username and not (VALID_NAME.match(username)):
        response.username = f"Invalid account name: {username!r}"
    elif username and (taken := store.taken_name(username)) is not None:
        response.username = f"Account '{taken}' already exists"
    if email.strip() and _email_taken(email=email, request=request, username=username):
        response.email = _EMAIL_TAKEN_MESSAGE
    return response


@router.get("/me", response_model=AccountResponse)
async def get_current_user_account(
    user: CurrentUser,
    request: Request,
):
    """Return the authenticated user's account. No extra scope required.

    An account on an issued password reads back with no groups: its session holds
    no standing until the change, and a console copies this read into its session.
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    account = _require_own_account(store, user.user_id)
    response = _account_response(
        account=account,
        groups=request.app.state.group_store.list(),
        lookups=_lookups(request),
        request=request,
    )
    if account.password_change_required:
        response.groups = []
        response.admin = False
    return response


# Declared, since the path alone names the same action as GET /me.
@router.put("/me", response_model=AccountResponse, openapi_extra=scalo_extra(verb="update-me"))
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
    existing = _require_own_account(store, user.user_id)
    fields = _contact_updates(body, existing, vouched=False)
    if "email_verified" in fields:
        _refuse_taken_email(email=body.email or "", request=request, username=existing.username)
    account = (
        store.update(existing.username, allow_protected=False, **fields) if fields else existing
    )
    await _persist_account(
        request,
        settings,
        username=account.username,
        account=account,
        summary="update own account",
        actor=user.user_id,
    )
    audit_account_change(user.user_id, account.username, "updated", {"fields": sorted(fields)})
    return _account_response(
        account=account,
        groups=request.app.state.group_store.list(),
        lookups=_lookups(request),
        request=request,
    )


@router.get(
    "/{username}",
    response_model=AccountResponse,
    dependencies=[Depends(require_own_org_action(scopes_dict["account_read"]))],
)
async def get_account(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single account by username (admin only).

    Soft-deleted (anonymized) accounts, and those the caller does not administer, answer 404 the same as a missing name.
    """
    account = _require_reachable(
        action=scopes_dict["account_read"], request=request, user=user, username=username
    )
    return _account_response(
        account=account,
        groups=request.app.state.group_store.list(),
        lookups=_lookups(request),
        request=request,
    )


def _refuse_account_update(
    *, body: UpdateAccountRequest, request: Request, user, username: str
) -> tuple[Account, list[str] | None]:
    """Raise every refusal an account update answers before it writes, and return the stored account and the groups it will hold, or None when its groups do not change."""
    from dfe_engine.auth import admin_retirement

    store = request.app.state.account_store
    group_store = request.app.state.group_store
    action = scopes_dict["account_write"]
    existing = _require_reachable(action=action, request=request, user=user, username=username)
    if (body.org is not None) and (body.org != existing.org):
        refuse_unreachable_org(
            action=action, noun="accounts", org=body.org, request=request, user=user
        )
    deactivates = (body.enabled is False) or (body.blocked is True)
    if deactivates and is_own_account(request=request, user=user, username=username):
        raise ProblemError(403, "Cannot deactivate your own account", code="self_deactivate")
    bindings = provider_bindings(request)
    if admin_retirement.locks_out_last_admin(
        existing,
        store,
        group_store,
        bindings=bindings,
        enabled=body.enabled,
        blocked=body.blocked,
    ):
        raise ProblemError(
            409,
            f"Cannot deactivate '{username}': it is the last usable account "
            "with the system admin role",
            code="last_admin",
        )
    held_after = None
    if body.groups is not None:
        groups = group_store.list()
        listed = {group.name for group in groups if username in group.members}
        held_after = groups_held_after(
            account=existing.model_copy(update={"groups": body.groups}),
            added=body.groups,
            bindings=bindings,
            groups=groups,
            removed=listed - set(body.groups),
        )
        check_group_changes(
            after=held_after,
            before=groups_held(account=existing, bindings=bindings, groups=groups),
            groups=groups,
            request=request,
            user=user,
        )
        if admin_retirement.locks_out_last_admin_by_group_loss(
            existing,
            store,
            group_store,
            bindings=bindings,
            held_after=held_after,
        ):
            raise ProblemError(
                409,
                f"Cannot remove the system admin group from '{username}': "
                "it is the last usable account with the system admin role",
                code="last_admin",
            )
    if (body.groups is not None) or (body.org is not None):
        stored_groups = group_store.list()
        held = (
            held_after
            if held_after is not None
            else groups_held(account=existing, bindings=bindings, groups=stored_groups)
        )
        refuse_org_role_breach(
            account_org=body.org if body.org is not None else existing.org,
            groups=[group for group in stored_groups if group.name in set(held)],
            request=request,
            username=username,
        )
    after = existing.model_copy(
        update={
            field: value
            for field, value in (
                ("blocked", body.blocked),
                ("enabled", body.enabled),
                ("groups", body.groups),
                ("org", body.org),
            )
            if value is not None
        }
    )
    _refuse_last_org_admin(
        after=after, before=existing, held_after=held_after, request=request, user=user
    )
    if "email_verified" in _contact_updates(body, existing, vouched=True):
        _refuse_taken_email(email=body.email or "", request=request, username=username)
    if body.org is not None:
        refuse_unknown_orgs(request, [body.org])
    return existing, held_after


async def refuse_account_update(
    body: UpdateAccountRequest, request: Request, user: CurrentUser, username: str
) -> None:
    """FastAPI dependency: answer an update's own refusals before its re-auth grant is asked for."""
    _refuse_account_update(body=body, request=request, user=user, username=username)


@router.put(
    "/{username}",
    response_model=AccountResponse,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["account_write"])),
        Depends(refuse_account_update),
        Depends(require_account_reauth),
    ],
    responses={
        403: {
            "description": "self_deactivate: an account cannot disable or block itself",
        },
        409: {
            "description": (
                "last_admin: disabling, blocking, or stripping the system admin "
                "group from this account would leave no usable system admin"
            ),
        },
    },
)
async def update_account(
    username: str,
    body: UpdateAccountRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
):
    """Update account groups, enabled/blocked status, or contact fields (admin only).

    Refused when the caller disables or blocks their own account, or when that
    (or stripping every system-scope admin group) would leave no usable
    system-admin account.
    """
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_group_members_for_account_groups_change

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    existing, held_after = _refuse_account_update(
        body=body, request=request, user=user, username=username
    )
    update_fields: dict[str, object] = _contact_updates(body, existing, vouched=True)
    if body.groups is not None:
        update_fields["groups"] = body.groups
    if body.enabled is not None:
        update_fields["enabled"] = body.enabled
    if body.blocked is not None:
        update_fields["blocked"] = body.blocked
    if body.org is not None:
        update_fields["org"] = body.org
    if body.groups is not None:
        # The group files decide membership, so diff against them: re-sending a list repairs drift.
        listed_in = {g.name for g in group_store.list() if username in g.members}
        new_groups = set(body.groups)
        account = store.update(username, allow_protected=False, **update_fields)
        sync_group_members_for_account_groups_change(
            group_store,
            username,
            added=new_groups,
            removed=listed_in - new_groups,
        )
    else:
        account = store.update(username, allow_protected=False, **update_fields)
    if account.session_generation != existing.session_generation:
        await persist_session_end(request, account, actor=user.user_id)
    await _persist_account(
        request,
        settings,
        username=account.username,
        account=account,
        summary="update account",
        actor=user.user_id,
    )
    # Field names only for the contact fields: their values are personal data.
    details: dict[str, object] = {"fields": sorted(update_fields)}
    for name in ("groups", "enabled", "blocked", "org"):
        if name in update_fields:
            details[name] = update_fields[name]
    audit_account_change(user.user_id, account.username, "updated", details)
    return _account_response(
        account=account,
        groups=group_store.list(),
        lookups=_lookups(request),
        request=request,
    )


# Declared, since the path alone names the same action as the admin reset on /{username}.
@router.post(
    "/reset-password",
    status_code=200,
    response_model=ResetPasswordResponse,
    openapi_extra=scalo_extra(verb="change-password"),
    responses={
        403: {
            "description": "current_password is not the account's password "
            "(invalid_current_password)",
        },
        429: {
            "description": "Too many wrong current passwords; Retry-After gives the "
            "seconds to wait",
        },
    },
)
async def reset_current_user_password(
    body: ChangeOwnPasswordRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> ResetPasswordResponse:
    """Change the authenticated user's own password, given the current one.

    The username is taken from the session, not the request, so a caller cannot
    reset another account through this route. ``current_password`` must be the
    account's password, so a stolen session token cannot take the account over;
    wrong guesses back off as sign-in failures do. An IdP-owned (``external``)
    account is refused: it has no local password. The live store takes the new
    password immediately and every session the account held ends, this one
    included, so the owner signs in again. The ``git`` block reports whether the
    durable mirror merged, is pending review, or is a no-op for a non-git-backed
    account.

    An account on an issued password may call this and nothing else that
    changes state, and the reset clears ``password_change_required``.
    """
    store = request.app.state.account_store
    account = _require_own_account(store, user.user_id)
    _refuse_external(account)
    throttle: LoginThrottle = request.app.state.login_throttle
    client_ip = _get_client_ip(request)
    wait = throttle.retry_after(account.username, client_ip)
    if wait:
        raise throttled(request, wait)
    if not await run_blocking(store.verify_password, account.username, body.current_password):
        throttle.failed(account.username, client_ip)
        audit_login_denied(user.user_id, "password_change", client_ip, "wrong_current_password")
        raise ProblemError(
            403, "That is not this account's current password.", code="invalid_current_password"
        )
    throttle.succeeded(account.username)
    result = await _reset_stored_password(
        request,
        settings,
        username=account.username,
        new_password=body.new_password,
        actor=user.user_id,
    )
    audit_account_change(user.user_id, account.username, "password_changed")
    return result


@router.post(
    "/{username}/reset-password",
    status_code=200,
    response_model=ResetPasswordResponse,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["accounts_reset_password"])),
        Depends(require_account_reauth),
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
    _require_reachable(
        action=scopes_dict["accounts_reset_password"],
        request=request,
        user=user,
        username=username,
    )
    result = await _reset_stored_password(
        request,
        settings,
        username=username,
        new_password=body.new_password,
        actor=user.user_id,
    )
    audit_account_change(user.user_id, username, "password_reset")
    return result


@router.post(
    "/{username}/rotate-password",
    status_code=200,
    response_model=RotatePasswordResponse,
    dependencies=[
        Depends(require_action(scopes_dict["accounts_reset_password"])),
        Depends(require_account_reauth),
    ],
)
async def rotate_password(
    username: str,
    body: RotatePasswordRequest,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> RotatePasswordResponse:
    """Rotate the local admin's password in the deployment's secret store.

    The store that injects ``DFE_ENGINE_AUTH_LOCAL_ADMIN_PASSWORD`` is the source of that
    password, so the engine writes the new value through its secrets backend and
    never into its own YAML. The next boot reconcile issues the rotated value to
    the admin, who must replace it at the following login. Returns 501 with the
    command that changes it where it is kept when the deployment has not declared
    a secrets path for the password. Every other account, ``breakglass`` included,
    changes its password through reset-password.
    """
    from dfe_engine.auth.deployment_hints import detect_deploy_kind, rotation_store_command

    local = settings.auth.local
    if username != admin_account_name(local.admin_name):
        raise ProblemError(
            400,
            f"'{username}' does not get its password from the deployment's "
            "secret store, so there is nothing to rotate. Reset its password "
            "instead.",
            code="not_store_backed",
        )
    if not local.admin_password_secret_path:
        command = rotation_store_command(
            detect_deploy_kind(settings.deployment.target),
            namespace=settings.deployment.namespace,
            secret_name=local.admin_secret_name,
            secret_key=local.admin_secret_key,
            deployer_command=settings.deployment.rotation_command,
        )
        raise ProblemError(
            501,
            "This deployment keeps the admin password outside DFE, so change "
            f"it where it is stored: {command}",
            code="admin_password_kept_outside_dfe",
            store_command=command,
        )

    store = secrets_store(request)
    path = local.admin_password_secret_path
    await run_blocking(lambda: store.put(path, body.new_password))
    audit_account_change(user.user_id, username, "rotated password in the secret store")
    return RotatePasswordResponse(secret_path=local.admin_password_secret_path)


@router.get(
    "/{username}/git-status",
    openapi_extra=scalo_extra(verb="git-status"),
    response_model=AccountGitState,
    dependencies=[Depends(require_platform_action(scopes_dict["account_read"]))],
)
async def account_git_status(
    username: str,
    user: CurrentUser,
    request: Request,
    settings: Settings,
) -> AccountGitState:
    """Poll whether an account's password has merged to the deploy repo's main.

    Step 2 of the review-PR path for the local admin and ``breakglass``: the UI
    polls this after the operator merges the PR. It fetches remote main and flips
    ``merged`` true. Durable immediately in the auto-merge and file-share modes. A
    regular user is never git-persisted, so it reports not-git-backed (durable in
    its own store).
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    _require_account(store, username)
    gc = getattr(request.app.state, "gitcrud", None)
    # Each poll fetches the deploy repo's remote, so it runs on a worker thread.
    if username == breakglass.USERNAME:
        return await run_blocking(
            functools.partial(
                account_durability.breakglass_state,
                gc,
                store,
                environment=settings.env,
                mode=settings.gitops.mode,
            )
        )
    if not account_durability.is_local_admin(username, settings.auth.local.admin_name):
        return account_durability.not_git_backed_state()
    return await run_blocking(
        functools.partial(
            account_durability.remote_state,
            gc,
            store,
            username,
            environment=settings.env,
            mode=settings.gitops.mode,
            forge=getattr(request.app.state, "forge", None),
        )
    )


@router.delete(
    "/{username}",
    status_code=204,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["account_write"])),
        Depends(require_account_reauth),
    ],
    responses={
        403: {
            "description": "self_delete: an account cannot delete itself",
        },
        409: {
            "description": "last_admin: deleting this account would leave no usable system admin",
        },
    },
)
async def delete_account(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Delete an account (admin only).

    Refused when the caller targets their own account, when the account holds the
    system admin role and no other usable admin-role account would remain
    (bootstrap admin and break-glass count when they can sign in), or when the
    store refuses a recovery credential.
    """
    from dfe_engine.auth import admin_retirement
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = _require_reachable(
        action=scopes_dict["account_write"], request=request, user=user, username=username
    )
    if is_own_account(request=request, user=user, username=username):
        raise ProblemError(403, "Cannot delete your own account", code="self_delete")
    _refuse_last_org_admin(after=None, before=existing, held_after=None, request=request, user=user)
    # Deleting an account takes it out of every group it holds, so it needs their roles.
    groups = group_store.list()
    bindings = provider_bindings(request)
    held = groups_held(existing, groups, bindings=bindings)
    check_group_changes(after=(), before=held, groups=groups, request=request, user=user)
    if admin_retirement.account_holds_system_admin(existing, groups, bindings=bindings):
        if not admin_retirement.other_usable_admin_exists(
            store, group_store, bindings=bindings, excluding=username
        ):
            raise ProblemError(
                409,
                f"Cannot delete '{username}': it is the last usable account "
                "with the system admin role",
                code="last_admin",
            )
    # The store refuses the local admin and breakglass, the accounts the deploy repo carries.
    store.delete(username)
    delete_link_records(store, existing)
    forget_member(group_store, username)
    audit_account_change(user.user_id, username, "deleted")


def delete_org_accounts(*, actor: str, org: str, request: Request) -> list[str]:
    """Delete every account bound to *org*, as a single account delete does, once the org itself goes."""
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    doomed = [
        account
        for account in store.list()
        if (account.org == org)
        and (not (account.is_link_record))
        and (not (account.is_soft_deleted))
    ]
    for account in doomed:
        store.delete(account.username)
        delete_link_records(store, account)
        forget_member(group_store, account.username)
        audit_account_change(actor, account.username, "deleted")
    return [account.username for account in doomed]


@router.delete(
    "/{username}/identities/{key}",
    status_code=204,
    dependencies=[
        Depends(require_own_org_action(scopes_dict["account_write"])),
        Depends(require_account_reauth),
    ],
    responses={
        404: {"description": "not_found: no such account, or it lists no sign-in under key"},
        409: {
            "description": "last_admin: the sign-in gives the last usable account its "
            "system admin role"
        },
    },
)
async def unlink_identity(username: str, key: str, user: CurrentUser, request: Request):
    """Unlink an IdP sign-in from an account (admin only).

    The sign-in's record becomes an account of its own, which its next sign-in judges on its own groups. Every session of the account ends, so one opened through the sign-in does not outlive the link. Taking away the groups the sign-in linked needs the roles they carry, as removing a group does.
    """
    from dfe_engine.auth import admin_retirement

    store = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = _require_reachable(
        action=scopes_dict["account_write"], request=request, user=user, username=username
    )
    if linked_identity(account=existing, key=key) is None:
        raise ProblemError(
            404, f"Account {username!r} has no linked sign-in {key!r}", code="not_found"
        )
    remaining = without_identity(identities=existing.identities, key=key)
    groups = group_store.list()
    bindings = provider_bindings(request)
    held_after = groups_held(
        account=existing.model_copy(update={"identities": remaining}),
        bindings=bindings,
        groups=groups,
    )
    check_group_changes(
        after=held_after,
        before=groups_held(account=existing, bindings=bindings, groups=groups),
        groups=groups,
        request=request,
        user=user,
    )
    if admin_retirement.locks_out_last_admin_by_group_loss(
        existing, store, group_store, bindings=bindings, held_after=held_after
    ):
        raise ProblemError(
            409,
            f"Cannot unlink {key!r} from {username!r}: it gives the last usable account "
            "its system admin role",
            code="last_admin",
        )
    # The record goes first, so a failure after it leaves the sign-in unlinked rather than re-linked at its next login.
    record = store.get(key)
    if (record is not None) and (record.linked_to == username):
        store.update(key, linked_to="")
    store.update(username, identities=remaining)
    account = store.end_sessions(username)
    await persist_session_end(request, account, actor=user.user_id)
    audit_identity_unlinked(account=username, admin_id=user.user_id, key=key)


# -- Attributes -----------------------------------------------
#
# Non-sensitive attributes ride inline on the Account model, so they are read
# and written through the account store. Sensitive attributes live in a separate
# keyed store (never inline, so a broad account read cannot leak them). Both
# sensitive endpoints still require the account to exist first.


@router.get(
    "/{username}/attributes",
    dependencies=[Depends(require_platform_action(scopes_dict["account_attributes_read"]))],
)
async def get_account_attributes(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Read an account's non-sensitive attribute blob (404 if missing or soft-deleted)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    account = _require_account(store, username)
    return {"attributes": account.attributes}


@router.put(
    "/{username}/attributes",
    dependencies=[
        Depends(require_action(scopes_dict["account_attributes_write"])),
        Depends(require_account_reauth),
    ],
)
async def put_account_attributes(
    username: str,
    body: AttributesRequest,
    user: CurrentUser,
    request: Request,
):
    """Full-replace an account's non-sensitive attribute blob (404 if missing or soft-deleted)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    _require_account(store, username)
    account = store.set_attributes(username, body.attributes)
    audit_account_change(user.user_id, username, "attributes_updated")
    return {"attributes": account.attributes}


@router.get(
    "/{username}/sensitive-attributes",
    dependencies=[
        Depends(require_platform_action(scopes_dict["account_attributes_read_sensitive"]))
    ],
)
async def get_account_sensitive_attributes(
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Read an account's SENSITIVE attribute blob from the separate keyed store.

    The account must exist first (404 otherwise), so a sensitive read cannot be
    used to probe for accounts that are not there. Soft-deleted accounts answer
    the same way.
    """
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    _require_account(store, username)
    return {"attributes": request.app.state.account_sensitive_attributes.get(username)}


@router.put(
    "/{username}/sensitive-attributes",
    dependencies=[
        Depends(require_action(scopes_dict["account_attributes_write_sensitive"])),
        Depends(require_account_reauth),
    ],
)
async def put_account_sensitive_attributes(
    username: str,
    body: AttributesRequest,
    user: CurrentUser,
    request: Request,
):
    """Full-replace an account's SENSITIVE attribute blob (404 if missing or soft-deleted)."""
    from dfe_engine.auth.accounts import AccountStore

    store: AccountStore = request.app.state.account_store
    _require_account(store, username)
    request.app.state.account_sensitive_attributes.put(username, body.attributes)
    audit_account_change(user.user_id, username, "sensitive_attributes_updated")
    return {"attributes": request.app.state.account_sensitive_attributes.get(username)}
