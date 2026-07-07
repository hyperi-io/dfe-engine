#  Project:      dfe-engine
#  File:         api/v1/account_groups.py
#  Purpose:      Group CRUD REST endpoints (scope-aware)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Group management router — CRUD for RBAC groups.

POST   /api/v1/auth/groups                           → Create group
GET    /api/v1/auth/groups                           → List groups
GET    /api/v1/auth/groups/{name}                    → Get group detail
PUT    /api/v1/auth/groups/{name}                    → Update group
POST   /api/v1/auth/groups/{name}/members            → Add member
DELETE /api/v1/auth/groups/{name}/members/{username}  → Remove member
DELETE /api/v1/auth/groups/{name}                    → Delete group

Groups carry a scope: ``system`` (spans orgs) or ``org:<name>``
(exists only inside that org). Checks run at the group's scope, so a
system-scope group:write holder manages everything, while an org-scope
group:write holder manages only that org's groups. Visibility follows
the same rule, plus members always see the groups they belong to —
org-local groups are never listed outside their org.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, check_action, is_action_allowed
from dfe_engine.api.pagination import (
    PaginatedResponse,
    PaginationParams,
    apply_search,
    apply_sort,
)
from dfe_engine.auth import Scope
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.groups import Group, GroupStore, validate_group_scope
from dfe_engine.auth.rbac_scopes import scopes_dict

router = APIRouter(prefix="/groups", tags=["Groups"])


# ── Request / Response models ────────────────────────────────


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


# ── Helpers ──────────────────────────────────────────────────


def _get_group_or_404(store: GroupStore, name: str) -> Group:
    """Return the named group or raise the standard 404 (shared by every by-name endpoint)."""
    group = store.get(name)
    if group is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    return group


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


# ── Endpoints ────────────────────────────────────────────────


@router.post("", response_model=GroupResponse, status_code=201)
async def create_group(
    body: CreateGroupRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new RBAC group at a scope."""
    from dfe_engine.auth.accounts import AccountStore
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
    sync_account_groups_for_membership_change(
        account_store,
        group.name,
        added=group.members,
    )
    audit_resource_change(user.user_id, "group", group.name, "created")
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
    store: GroupStore = request.app.state.group_store
    visible = [g for g in store.list() if _visible(request, user, g)]
    rows = [
        {
            "name": g.name,
            "description": g.description,
            "roles": g.roles,
            "members": g.members,
            "scope": g.scope,
        }
        for g in visible
    ]
    rows = apply_search(rows, search, ["name", "description"])
    rows = apply_sort(rows, sort_by, sort_order)
    summaries = [GroupResponse(**row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get("/{name}", response_model=GroupResponse)
async def get_group(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single group by name (404 when not visible to the caller)."""
    store: GroupStore = request.app.state.group_store
    group = _get_group_or_404(store, name)
    if not _visible(request, user, group):
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
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = _get_group_or_404(store, name)
    check_action(request, user, scopes_dict["group_write"], scope=_scope_of(existing))
    update_fields: dict[str, object] = {}
    if body.roles is not None:
        update_fields["roles"] = body.roles
    if body.description is not None:
        update_fields["description"] = body.description
    if body.members is not None:
        # dict.fromkeys preserves first-seen order while dropping duplicates.
        deduped = list(dict.fromkeys(body.members))
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
    audit_resource_change(user.user_id, "group", group.name, "updated")
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
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = _get_group_or_404(store, name)
    check_action(request, user, scopes_dict["group_add_member"], scope=_scope_of(existing))
    store.add_member(name, body.username)
    sync_account_groups_for_membership_change(
        account_store,
        name,
        added=[body.username],
    )
    audit_resource_change(user.user_id, "group_member", f"{name}/{body.username}", "added")
    group = store.get(name)
    return _response(group if group is not None else existing)


@router.delete("/{name}/members/{username}", response_model=GroupResponse, status_code=200)
async def remove_member(
    name: str,
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Remove a member from a group (checked at the group's scope)."""
    from dfe_engine.auth.accounts import AccountStore
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store: GroupStore = request.app.state.group_store
    account_store: AccountStore = request.app.state.account_store
    existing = _get_group_or_404(store, name)
    check_action(request, user, scopes_dict["group_remove_member"], scope=_scope_of(existing))
    store.remove_member(name, username)
    sync_account_groups_for_membership_change(
        account_store,
        name,
        removed=[username],
    )
    audit_resource_change(user.user_id, "group_member", f"{name}/{username}", "removed")
    # Membership -> HyperDX propagation: if this removal leaves the account in NO
    # groups it has lost all access, so revoke its HyperDX membership everywhere
    # (non-fatal). The partial case - still in other groups but losing one org -
    # is not handled here (needs per-org org_ids resolution); the account
    # disable/delete path covers full-access loss. Keyed by the stored email.
    account = account_store.get(username)
    if account is not None and not account.groups:
        lifecycle = getattr(request.app.state, "org_lifecycle", None)
        if lifecycle is not None and account.email:
            await lifecycle.revoke_member_everywhere(account.email)
    group = store.get(name)
    return _response(group if group is not None else existing)


@router.delete("/{name}", status_code=204)
async def delete_group(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Delete a group (checked at the group's scope)."""
    store: GroupStore = request.app.state.group_store
    existing = _get_group_or_404(store, name)
    check_action(request, user, scopes_dict["group_delete"], scope=_scope_of(existing))
    try:
        store.delete(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    audit_resource_change(user.user_id, "group", name, "deleted")
