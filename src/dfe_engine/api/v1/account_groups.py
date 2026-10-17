#  Project:      dfe-engine
#  File:         api/v1/account_groups.py
#  Purpose:      Group CRUD REST endpoints (scope-aware)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Group management router -- CRUD for RBAC groups.

POST   /api/v1/auth/groups                           -> Create group
GET    /api/v1/auth/groups                           -> List groups
GET    /api/v1/auth/groups/{name}                    -> Get group detail
PUT    /api/v1/auth/groups/{name}                    -> Update group
POST   /api/v1/auth/groups/{name}/members            -> Add member
DELETE /api/v1/auth/groups/{name}/members/{username}  -> Remove member
DELETE /api/v1/auth/groups/{name}                    -> Delete group

Groups carry a scope: ``system`` (spans orgs) or ``org:<name>``
(exists only inside that org). Checks run at the group's scope, so a
system-scope group:write holder manages everything, while an org-scope
group:write holder manages only that org's groups. Visibility follows
the same rule, plus members always see the groups they belong to --
org-local groups are never listed outside their org.

A group's ``source_id`` and ``source_provider`` link it to an IdP group: a login asserting that id takes the group's roles, so setting or moving the link needs the role rights adding a member does. No two groups may carry one id, and no group's name may be another group's id either, since an API key resolves a group identifier by name first, then by id. A provider id therefore names one group across every org, so setting or changing a link needs group:write system-wide.

A group the caller cannot see answers every route as a missing one, and a caller that cannot see every group is told only that a name is not available, so no answer says a group outside its scope exists.

A membership change is mirrored onto a local account's ``groups`` list, never onto an IdP-owned account's, which holds what its IdP asserts.
"""

import unicodedata
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from scalo.logger import logger

from dfe_engine.api.deps import (
    CurrentUser,
    bound_account,
    check_action,
    groups_granting,
    is_action_allowed,
    live_role_config,
    provider_bindings,
    require_action,
)
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth import AuthorizationError, RoleConfig, Scope
from dfe_engine.auth.audit import audit_group_change, audit_permission_denied
from dfe_engine.auth.groups import (
    Group,
    GroupExistsError,
    identifier_holder,
    source_id_holder,
    validate_group_scope,
)
from dfe_engine.auth.models import ungrantable_roles
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.scim_mapping import SCIM_SOURCE_PROVIDER
from dfe_engine.governance.ch import request_ch_rbac_reconcile
from dfe_engine.names import StoreName
from dfe_engine.yaml_utils import YAMLError

router = APIRouter(prefix="/groups", tags=["Groups"])

_LINK_FIELD_MAX_LENGTH = 512

_SOURCE_ID_DESCRIPTION = (
    "The group identifier the identity provider asserts in its tokens: the group name for "
    "Okta, dex and Keycloak groups claims, the object ID for Entra. A login through "
    "source_provider asserting it takes this group's roles. Empty for a group managed only "
    "here. A non-empty source ID needs a source_provider. At most 512 characters, with no "
    "control characters and no leading or trailing whitespace; no other group may carry it."
)
_SOURCE_PROVIDER_DESCRIPTION = (
    "The provider whose logins the source ID answers, required with a source ID: an OIDC "
    "provider name, a key or value of auth.source_provider_bindings, 'scim' or, while "
    "auth.trust_proxy_auth_headers is on, auth.proxy_provider. At most 512 characters."
)


# -- Request / Response models --------------------------------


class CreateGroupRequest(BaseModel):
    name: StoreName = Field(description="Unique group name")
    roles: list[str] = Field(description="Roles assigned to all group members")
    description: str = Field("", description="Human-readable description")
    scope: str = Field(
        "system",
        description="'system' (roles bind system-wide) or 'org:<name>' "
        "(group exists only inside that org; roles bind at that org's scope)",
    )
    members: list[str] = Field(
        default_factory=list,
        description="Account usernames in this group (local login resolves roles from this list)",
    )
    source_id: str = Field("", description=_SOURCE_ID_DESCRIPTION)
    source_provider: str = Field("", description=_SOURCE_PROVIDER_DESCRIPTION)


class UpdateGroupRequest(BaseModel):
    roles: list[str] | None = Field(None, description="Replace role list")
    description: str | None = Field(None, description="Replace description")
    members: list[str] | None = Field(None, description="Replace member username list")
    source_id: str | None = Field(
        None, description=f"Replace the source ID; omit to keep it. {_SOURCE_ID_DESCRIPTION}"
    )
    source_provider: str | None = Field(
        None,
        description=f"Replace the source provider; omit to keep it. {_SOURCE_PROVIDER_DESCRIPTION}",
    )


class AddMemberRequest(BaseModel):
    username: str = Field(description="Account username to add")


class GroupResponse(BaseModel):
    name: str
    description: str
    roles: list[str]
    members: list[str]
    scope: str
    source_id: str
    source_provider: str


class AttributesRequest(BaseModel):
    """Full-replace body for a group's attribute blob (non-sensitive or sensitive)."""

    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def _bounded_depth(cls, value: dict[str, Any]) -> dict[str, Any]:
        from dfe_engine.auth.attributes import check_attribute_depth

        return check_attribute_depth(value)


# -- Helpers --------------------------------------------------


def scope_of(group: Group) -> Scope:
    """The scope *group*'s roles bind at: its owning org's, else system-wide."""
    org = group.scope_org
    return Scope(type="org", id=org) if org else Scope()


def _member_name(request: Request, user) -> str | None:
    """The username the session's own account is listed under in group members, if any."""
    account = bound_account(request, user.user_id)
    return account.username if account is not None else None


def _held(request: Request, user) -> set[str]:
    """The names of the groups the session's roles come from, resolved as its roles were.

    An identifier that names one group and is another's provider id holds the first alone.
    """
    return set(groups_granting(user.groups, request.app.state.group_store))


def _visible(request: Request, user, group: Group, member: str | None, held: set[str]) -> bool:
    """Whether the session sees *group*: always its own, else by group:read at the group's scope.

    Org-local groups stay invisible outside their org. A member is listed in the group
    file, or holds the group through its session's groups (*held*): an identity provider
    asserts those, and an API key carries its own.
    """
    if member is not None and member in group.members:
        return True
    if group.name in held:
        return True
    return is_action_allowed(request, user, scopes_dict["group_read"], scope=scope_of(group))


def _not_found(name: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"Group '{name}' not found"},
    )


def _visible_group(request: Request, user, name: str) -> Group:
    """The stored group *name* when the session sees it (:func:`_visible`).

    Raises:
        HTTPException: The 404 a missing group answers, for one outside the caller's scope too.
    """
    group = request.app.state.group_store.get(name)
    if group is None or not _visible(
        request, user, group, _member_name(request, user), _held(request, user)
    ):
        raise _not_found(name)
    return group


def _refuse_link_below_system_scope(request: Request, user) -> None:
    """Refuse a link change from a caller without group:write system-wide.

    A provider id names one group across every org, so an org-scoped writer that could
    set one could take an id a system group needs, and read in a refusal whether
    another org already holds it.

    Raises:
        AuthorizationError: 403 before any other group is looked at.
    """
    if is_action_allowed(request, user, scopes_dict["group_write"]):
        return
    reason = (
        "linking a group to an identity provider needs group:write system-wide, "
        "since a provider id names one group across every org"
    )
    audit_permission_denied(user.user_id, scopes_dict["group_write"], user.roles, reason)
    raise AuthorizationError(reason)


def _response(group: Group) -> GroupResponse:
    return GroupResponse(
        description=group.description,
        members=group.members,
        name=group.name,
        roles=group.roles,
        scope=group.scope,
        source_id=group.source_id,
        source_provider=group.source_provider,
    )


def _is_known_source_provider(*, name: str, request: Request) -> bool:
    """Whether a login can answer for provider *name*: SCIM's stamp, a binding, a registered OIDC provider or the proxy provider while the proxy headers are trusted."""
    if name == SCIM_SOURCE_PROVIDER:
        return True
    bindings = provider_bindings(request)
    if (name in bindings) or (name in bindings.values()):
        return True
    settings = getattr(request.app.state, "settings", None)
    # The proxy provider answers logins only through the trusted X-Oidc-* headers.
    if (
        settings is not None
        and settings.auth.trust_proxy_auth_headers
        and name == settings.auth.proxy_provider
    ):
        return True
    registry = getattr(request.app.state, "oidc_provider_registry", None)
    if registry is None:
        return False
    # A provider file that does not load answers for no login, so it names no known provider.
    try:
        return registry.get(name) is not None
    except (OSError, UnicodeDecodeError, YAMLError, RecursionError, ValueError) as exc:
        # The class alone: a parse error's text can quote the file.
        logger.warning(
            "OIDC provider file did not load, so a group cannot link to it",
            error_type=type(exc).__name__,
            provider=name,
        )
        return False


def _link_changes(
    *, before: Group | None, source_id: str, source_provider: str
) -> tuple[bool, bool]:
    """Whether a write leaving a group with *source_id* and *source_provider* changes each of them from *before*, the group as stored (None for a new one)."""
    stored_id = before.source_id if before is not None else ""
    stored_provider = before.source_provider if before is not None else ""
    return source_id != stored_id, source_provider != stored_provider


def _refuse_bad_link(
    *,
    changes_source_id: bool,
    changes_source_provider: bool,
    name: str,
    request: Request,
    source_id: str,
    source_provider: str,
) -> None:
    """Refuse a source ID or provider the request changes to one no login could use to reach group *name* alone.

    *source_id* and *source_provider* are the values the group would hold; *changes_source_id* and *changes_source_provider* say whether the write changes each (:func:`_link_changes`). Only a changed value is checked, so a stored value these rules would now refuse (a SCIM externalId, a deleted provider) never blocks an edit.
    """
    if changes_source_id:
        _refuse_malformed_source_id(source_id=source_id)
    if changes_source_provider and source_provider:
        _refuse_unknown_source_provider(name=source_provider, request=request)
    changes_link = (changes_source_id) or (changes_source_provider)
    if changes_link and source_id and not (source_provider):
        raise HTTPException(
            detail={
                "code": "missing_source_provider",
                "message": "a source_id needs a source_provider naming the provider whose logins it answers",
            },
            status_code=422,
        )
    if not (changes_source_id) or not (source_id):
        return
    groups = request.app.state.group_store.list()
    if source_id_holder(groups=groups, name=name, source_id=source_id) is not None:
        raise HTTPException(
            detail={
                "code": "conflict",
                "message": f"source_id {source_id!r} is already another group's",
            },
            status_code=409,
        )


def _refuse_identifier_collision(
    *, groups: list[Group], name: str, source_id: str, target: str
) -> None:
    """Refuse a write giving group *target* a *name* that is another group's source ID or a *source_id* that is another group's name (:func:`identifier_holder`); pass empty for the one the write leaves alone."""
    holder = identifier_holder(exclude=target, groups=groups, name=name, source_id=source_id)
    if holder is None:
        return
    if name and holder.source_id == name:
        message = f"name {name!r} is already another group's source_id"
    else:
        message = f"source_id {source_id!r} is already another group's name"
    raise HTTPException(detail={"code": "identifier_taken", "message": message}, status_code=409)


def _refuse_malformed_source_id(*, source_id: str) -> None:
    """Refuse a source ID no login could assert: one past the length cap, padded with whitespace or carrying a control character."""
    problem = ""
    if len(source_id) > _LINK_FIELD_MAX_LENGTH:
        problem = f"source_id is at most {_LINK_FIELD_MAX_LENGTH} characters"
    # Both login paths strip each asserted group, so a padded source ID could never match one.
    elif source_id != source_id.strip():
        problem = "source_id must not start or end with whitespace"
    elif any(unicodedata.category(character) == "Cc" for character in source_id):
        problem = "source_id must not contain control characters"
    if problem:
        raise HTTPException(
            detail={"code": "invalid_source_id", "message": problem}, status_code=422
        )


def _refuse_unknown_source_provider(*, name: str, request: Request) -> None:
    """Refuse a source provider no login answers for, echoing it only when it is within the length cap."""
    if len(name) > _LINK_FIELD_MAX_LENGTH:
        message = f"source_provider is at most {_LINK_FIELD_MAX_LENGTH} characters"
    elif not (_is_known_source_provider(name=name, request=request)):
        message = (
            f"source_provider {name!r} is not a provider a login answers for: use an OIDC "
            "provider name, an auth.source_provider_bindings entry, 'scim' or "
            "auth.proxy_provider while the proxy headers are trusted"
        )
    else:
        return
    raise HTTPException(
        detail={"code": "invalid_source_provider", "message": message}, status_code=422
    )


def check_role_assignment(request: Request, user, roles: Iterable[str], scope: Scope) -> None:
    """Refuse a change to who holds a group's roles unless the caller could grant them.

    Giving a group a role or taking one away, adding a member or removing one, and
    changing the provider id an IdP login resolves the group by all change who
    holds its roles. Each needs the caller to hold every permission those roles
    grant, at a scope covering the group's (:func:`~dfe_engine.auth.models.ungrantable_roles`).
    role:write defines roles and hands none out. Without this a group:write holder
    could make itself admin, or strip admin from everyone else, and an admin of one
    org could act as another's.

    Args:
        request: The request, for the role configuration and auth settings.
        user: The caller.
        roles: The roles whose holders the change affects.
        scope: The scope the group's roles bind at.

    Raises:
        AuthorizationError: 403 naming the roles the caller may not hand out or take away.
    """
    if not request.app.state.settings.auth.enabled:
        return
    role_config = live_role_config(request) or RoleConfig.load_builtin()
    missing = ungrantable_roles(user, roles, scope, role_config=role_config)
    if missing:
        reason = f"cannot grant or remove role(s) granting more than you hold: {missing}"
        audit_permission_denied(user.user_id, scopes_dict["role_write"], user.roles, reason)
        raise AuthorizationError(reason)


def check_group_changes(
    *, after: Iterable[str], before: Iterable[str], groups: list[Group], request: Request, user
) -> None:
    """Refuse an account write that changes the groups it holds unless the caller could grant each gained or lost group's roles.

    *before* and *after* are the names of the groups the account holds now and would hold once written (:func:`~dfe_engine.auth.membership.groups_held`, :func:`~dfe_engine.auth.membership.groups_held_after`), so a change by membership and one through an IdP-owned account's link are both seen. *groups* is every stored group, listed once per request.
    """
    by_name = {group.name: group for group in groups}
    for name in sorted(set(before) ^ set(after)):
        group = by_name.get(name)
        if group is not None:
            check_role_assignment(
                request=request, roles=group.roles, scope=scope_of(group=group), user=user
            )


def _refuse_last_admin_member_removals(
    request: Request,
    *,
    group_name: str,
    removed: Iterable[str],
) -> None:
    """Raise 409 when dropping *removed* from *group_name* leaves no usable system admin."""
    from dfe_engine.auth import admin_retirement

    dropped = sorted({name for name in removed if name})
    if not dropped:
        return
    if not admin_retirement.locks_out_last_admin_by_member_removals(
        request.app.state.account_store,
        request.app.state.group_store,
        bindings=provider_bindings(request),
        group_name=group_name,
        removed=dropped,
    ):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "last_admin",
            "message": f"Cannot remove the last usable system admin from '{group_name}'",
        },
    )


# -- Endpoints ------------------------------------------------


@router.post("", response_model=GroupResponse, status_code=201)
async def create_group(
    body: CreateGroupRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new RBAC group at a scope."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    try:
        validate_group_scope(body.scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_scope", "message": str(exc)},
        ) from exc

    target = Group(name=body.name, scope=body.scope)
    check_action(request, user, scopes_dict["group_write"], scope=scope_of(target))
    if body.source_id or body.source_provider:
        _refuse_link_below_system_scope(request, user)
    check_role_assignment(request, user, body.roles, scope_of(target))

    org = target.scope_org
    org_registry = getattr(request.app.state, "org_registry", None)
    # With no registry to check against, no org is known to exist.
    if org and (org_registry is None or org_registry.get(org) is None):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_scope", "message": f"Org '{org}' not found"},
        )

    changes_source_id, changes_source_provider = _link_changes(
        before=None, source_id=body.source_id, source_provider=body.source_provider
    )
    _refuse_bad_link(
        changes_source_id=changes_source_id,
        changes_source_provider=changes_source_provider,
        name=body.name,
        request=request,
        source_id=body.source_id,
        source_provider=body.source_provider,
    )
    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    # Names are unique across every org, so a caller that cannot see every group learns only this.
    unavailable = HTTPException(
        status_code=409,
        detail={"code": "conflict", "message": f"Group name '{body.name}' is not available"},
    )
    sees_every_group = is_action_allowed(request, user, scopes_dict["group_read"])
    try:
        _refuse_identifier_collision(
            groups=store.list(), name=body.name, source_id=body.source_id, target=body.name
        )
    except HTTPException as exc:
        if sees_every_group:
            raise
        raise unavailable from exc
    # create() decides, not get(): get() reports a stored group that does not load as absent.
    try:
        group = store.create(
            description=body.description,
            members=body.members,
            name=body.name,
            roles=body.roles,
            scope=body.scope,
            source_id=body.source_id,
            source_provider=body.source_provider,
        )
    except GroupExistsError as exc:
        if not sees_every_group:
            raise unavailable from exc
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Group '{body.name}' already exists"},
        ) from exc
    request_ch_rbac_reconcile(request.app.state)
    sync_account_groups_for_membership_change(
        account_store,
        group.name,
        added=group.members,
    )
    audit_group_change(
        user.user_id,
        group.name,
        "created",
        {
            "roles": group.roles,
            "scope": group.scope,
            "members": group.members,
            "source_id": group.source_id,
            "source_provider": group.source_provider,
        },
    )
    return _response(group)


@router.get("", response_model=PaginatedResponse[GroupResponse])
async def list_groups(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in group name/description"),
    sort_by: str | None = Query(None, description="Sort field (name, scope)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List groups visible to the caller (own memberships + scope grants), paginated."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    member = _member_name(request, user)
    held = _held(request, user)
    visible = [g for g in store.list() if _visible(request, user, g, member, held)]
    rows = [_response(g).model_dump() for g in visible]
    rows = apply_search(rows, search, ["name", "description"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [GroupResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get("/{name}", response_model=GroupResponse)
async def get_group(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single group by name (404 when not visible to the caller)."""
    return _response(_visible_group(request, user, name))


@router.put("/{name}", response_model=GroupResponse)
async def update_group(
    name: str,
    body: UpdateGroupRequest,
    user: CurrentUser,
    request: Request,
):
    """Update group roles, description, members or IdP link (group:write at the group's scope)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = _visible_group(request, user, name)
    check_action(request, user, scopes_dict["group_write"], scope=scope_of(existing))
    changes_members = body.members is not None and set(body.members) != set(existing.members)
    source_id = existing.source_id if body.source_id is None else body.source_id
    source_provider = (
        existing.source_provider if body.source_provider is None else body.source_provider
    )
    changes_source_id, changes_source_provider = _link_changes(
        before=existing, source_id=source_id, source_provider=source_provider
    )
    changes_link = (changes_source_id) or (changes_source_provider)
    if changes_link:
        _refuse_link_below_system_scope(request, user)
    if (body.roles is not None) or (changes_members) or (changes_link):
        # Members and linked logins keep, gain or lose the roles before and after; a role removed counts too.
        roles_after = body.roles if body.roles is not None else existing.roles
        check_role_assignment(request, user, [*existing.roles, *roles_after], scope_of(existing))
    _refuse_bad_link(
        changes_source_id=changes_source_id,
        changes_source_provider=changes_source_provider,
        name=name,
        request=request,
        source_id=source_id,
        source_provider=source_provider,
    )
    if changes_source_id:
        _refuse_identifier_collision(groups=store.list(), name="", source_id=source_id, target=name)
    update_fields: dict[str, object] = {}
    details: dict[str, object] = {}
    if body.roles is not None:
        update_fields["roles"] = body.roles
        details["roles"] = body.roles
    if body.description is not None:
        update_fields["description"] = body.description
    # A link resent unchanged is no change, so the audit's fields leave it out.
    if changes_source_id:
        update_fields["source_id"] = source_id
    if changes_source_provider:
        update_fields["source_provider"] = source_provider
    if changes_link:
        details["source_id"] = {"after": source_id, "before": existing.source_id}
        details["source_provider"] = {
            "after": source_provider,
            "before": existing.source_provider,
        }
    if body.members is not None:
        seen: set[str] = set()
        deduped: list[str] = []
        for username in body.members:
            if username not in seen:
                seen.add(username)
                deduped.append(username)
        update_fields["members"] = deduped
        old_members = set(existing.members)
        new_members = set(deduped)
        details["members_added"] = sorted(new_members - old_members)
        details["members_removed"] = sorted(old_members - new_members)
        # Recovery credentials before last-admin: a drop of admin/breakglass is 403, not 409.
        store.protected.check_members_replaced(name, existing.members, deduped)
        _refuse_last_admin_member_removals(
            request, group_name=name, removed=old_members - new_members
        )
        group = store.update(name, **update_fields)
        sync_account_groups_for_membership_change(
            account_store,
            name,
            added=new_members - old_members,
            removed=old_members - new_members,
        )
    else:
        group = store.update(name, **update_fields)
    request_ch_rbac_reconcile(request.app.state)
    audit_group_change(
        user.user_id, group.name, "updated", {"fields": sorted(update_fields), **details}
    )
    return _response(group)


@router.post("/{name}/members", response_model=GroupResponse, status_code=200)
async def add_member(
    name: str,
    body: AddMemberRequest,
    user: CurrentUser,
    request: Request,
):
    """Add a member to a group (idempotent; checked at the group's scope)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = _visible_group(request, user, name)
    check_action(request, user, scopes_dict["group_add_member"], scope=scope_of(existing))
    if body.username not in existing.members:
        # The new member takes every role the group carries.
        check_role_assignment(request, user, existing.roles, scope_of(existing))
    store.add_member(name, body.username)
    sync_account_groups_for_membership_change(
        account_store,
        name,
        added=[body.username],
    )
    audit_group_change(user.user_id, name, "member_added", {"member": body.username})
    group = store.get(name)
    if group is None:  # deleted concurrently between the mutation and re-fetch
        raise _not_found(name)
    return _response(group)


@router.delete("/{name}/members/{username}", response_model=GroupResponse, status_code=200)
async def remove_member(
    name: str,
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Remove a member from a group (checked at the group's scope)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = _visible_group(request, user, name)
    check_action(request, user, scopes_dict["group_remove_member"], scope=scope_of(existing))
    if username in existing.members:
        # The member loses every role the group carries.
        check_role_assignment(request, user, existing.roles, scope_of(existing))
        # Recovery credentials before last-admin: a drop of admin/breakglass is 403, not 409.
        store.protected.check_member_removal(name, [username])
        _refuse_last_admin_member_removals(request, group_name=name, removed=[username])
    store.remove_member(name, username)
    sync_account_groups_for_membership_change(
        account_store,
        name,
        removed=[username],
    )
    audit_group_change(user.user_id, name, "member_removed", {"member": username})
    group = store.get(name)
    if group is None:  # deleted concurrently between the mutation and re-fetch
        raise _not_found(name)
    return _response(group)


@router.delete("/{name}", status_code=204)
async def delete_group(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Delete a group (checked at the group's scope)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    existing = _visible_group(request, user, name)
    check_action(request, user, scopes_dict["group_delete"], scope=scope_of(existing))
    if (existing.members) or (existing.source_id):
        # Its members and every login its source ID answers lose the roles it carries.
        check_role_assignment(
            request=request, roles=existing.roles, scope=scope_of(group=existing), user=user
        )
    try:
        store.delete(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    request_ch_rbac_reconcile(request.app.state)
    audit_group_change(
        admin_id=user.user_id,
        change="deleted",
        details={"source_id": existing.source_id, "source_provider": existing.source_provider},
        group_name=name,
    )


# -- Attributes -----------------------------------------------
#
# Non-sensitive attributes ride inline on the Group model; sensitive attributes
# live in the separate keyed store (never inline, so a broad group read cannot
# leak them). Both sensitive endpoints require the group to exist first.


@router.get(
    "/{name}/attributes",
    dependencies=[Depends(require_action(scopes_dict["group_attributes_read"]))],
)
async def get_group_attributes(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Read a group's non-sensitive attribute blob (404 if the group is missing)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    group = store.get(name)
    if group is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    return {"attributes": group.attributes}


@router.put(
    "/{name}/attributes",
    dependencies=[Depends(require_action(scopes_dict["group_attributes_write"]))],
)
async def put_group_attributes(
    name: str,
    body: AttributesRequest,
    user: CurrentUser,
    request: Request,
):
    """Full-replace a group's non-sensitive attribute blob (404 if missing)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    try:
        group = store.set_attributes(name, body.attributes)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        ) from exc
    audit_group_change(user.user_id, name, "attributes_updated")
    return {"attributes": group.attributes}


@router.get(
    "/{name}/sensitive-attributes",
    dependencies=[Depends(require_action(scopes_dict["group_attributes_read_sensitive"]))],
)
async def get_group_sensitive_attributes(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Read a group's SENSITIVE attribute blob from the separate keyed store.

    The group must exist first (404 otherwise), so a sensitive read cannot probe
    for groups that are not there.
    """
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    return {"attributes": request.app.state.group_sensitive_attributes.get(name)}


@router.put(
    "/{name}/sensitive-attributes",
    dependencies=[Depends(require_action(scopes_dict["group_attributes_write_sensitive"]))],
)
async def put_group_sensitive_attributes(
    name: str,
    body: AttributesRequest,
    user: CurrentUser,
    request: Request,
):
    """Full-replace a group's SENSITIVE attribute blob (404 if the group is missing)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    request.app.state.group_sensitive_attributes.put(name, body.attributes)
    audit_group_change(user.user_id, name, "sensitive_attributes_updated")
    return {"attributes": request.app.state.group_sensitive_attributes.get(name)}
