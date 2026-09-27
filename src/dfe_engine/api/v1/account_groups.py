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
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, check_action, is_action_allowed, require_action
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth import Scope
from dfe_engine.auth.groups import Group, validate_group_scope
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.governance.ch import request_ch_rbac_reconcile

router = APIRouter(prefix="/groups", tags=["Groups"])


# -- Request / Response models --------------------------------


class CreateGroupRequest(BaseModel):
    name: str = Field(description="Unique group name")
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


class UpdateGroupRequest(BaseModel):
    roles: list[str] | None = Field(None, description="Replace role list")
    description: str | None = Field(None, description="Replace description")
    members: list[str] | None = Field(None, description="Replace member username list")


class AddMemberRequest(BaseModel):
    username: str = Field(description="Account username to add")


class GroupResponse(BaseModel):
    name: str
    description: str
    roles: list[str]
    members: list[str]
    scope: str


class AttributesRequest(BaseModel):
    """Full-replace body for a group's attribute blob (non-sensitive or sensitive)."""

    attributes: dict[str, Any] = Field(default_factory=dict)


# -- Helpers --------------------------------------------------


def _scope_of(group: Group) -> Scope:
    org = group.scope_org
    return Scope(type="org", id=org) if org else Scope()


def _visible(request: Request, user, group: Group) -> bool:
    """Members always see their own groups; otherwise group:read at the
    group's scope decides (org-local groups stay invisible outside their org)."""
    if user.user_id in group.members:
        return True
    return is_action_allowed(request, user, scopes_dict["group_read"], scope=_scope_of(group))


def _response(group: Group) -> GroupResponse:
    return GroupResponse(
        name=group.name,
        description=group.description,
        roles=group.roles,
        members=group.members,
        scope=group.scope,
    )


def _check_role_assignment(request: Request, user, roles: list[str], scope: Scope) -> None:
    """Guard against privilege escalation via group roles.

    A caller may put a role on a group only if they hold role-management
    permission (role:write) at the group's scope, or already hold that role
    themselves. Without this, a group:write holder could grant a group -- and
    thereby themselves, by joining it -- a role they do not have (e.g. admin).
    """
    if not roles:
        return
    if is_action_allowed(request, user, scopes_dict["role_write"], scope=scope):
        return
    held = set(user.roles)
    escalated = sorted(r for r in roles if r not in held)
    if escalated:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": (
                    f"Cannot assign role(s) you do not hold: {escalated}; requires role:write"
                ),
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
    check_action(request, user, scopes_dict["group_write"], scope=_scope_of(target))
    _check_role_assignment(request, user, body.roles, _scope_of(target))

    org = target.scope_org
    org_registry = getattr(request.app.state, "org_registry", None)
    if org and org_registry is not None and org_registry.get(org) is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_scope", "message": f"Org '{org}' not found"},
        )

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    if store.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Group '{body.name}' already exists"},
        )
    group = store.create(
        body.name,
        roles=body.roles,
        description=body.description,
        members=body.members,
        scope=body.scope,
    )
    request_ch_rbac_reconcile(request.app.state)
    sync_account_groups_for_membership_change(
        account_store,
        group.name,
        added=group.members,
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
    visible = [g for g in store.list() if _visible(request, user, g)]
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
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    group = store.get(name)
    if group is None or not _visible(request, user, group):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    return _response(group)


@router.put("/{name}", response_model=GroupResponse)
async def update_group(
    name: str,
    body: UpdateGroupRequest,
    user: CurrentUser,
    request: Request,
):
    """Update group roles, description, or members (group:write at the group's scope)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.groups import GroupStore
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = store.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    check_action(request, user, scopes_dict["group_write"], scope=_scope_of(existing))
    if body.roles is not None:
        _check_role_assignment(request, user, body.roles, _scope_of(existing))
    update_fields: dict[str, object] = {}
    if body.roles is not None:
        update_fields["roles"] = body.roles
    if body.description is not None:
        update_fields["description"] = body.description
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
    existing = store.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    check_action(request, user, scopes_dict["group_add_member"], scope=_scope_of(existing))
    store.add_member(name, body.username)
    sync_account_groups_for_membership_change(
        account_store,
        name,
        added=[body.username],
    )
    group = store.get(name)
    if group is None:  # deleted concurrently between the mutation and re-fetch
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
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
    existing = store.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    check_action(request, user, scopes_dict["group_remove_member"], scope=_scope_of(existing))
    store.remove_member(name, username)
    sync_account_groups_for_membership_change(
        account_store,
        name,
        removed=[username],
    )
    group = store.get(name)
    if group is None:  # deleted concurrently between the mutation and re-fetch
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
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
    existing = store.get(name)
    if existing is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    check_action(request, user, scopes_dict["group_delete"], scope=_scope_of(existing))
    try:
        store.delete(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    request_ch_rbac_reconcile(request.app.state)


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
    return {"attributes": request.app.state.group_sensitive_attributes.get(name)}
