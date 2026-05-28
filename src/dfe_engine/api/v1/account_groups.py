#  Project:      dfe-engine
#  File:         api/v1/account_groups.py
#  Purpose:      Group CRUD REST endpoints (admin only)
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

All endpoints require admin role (org:write).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action

router = APIRouter(prefix="/groups", tags=["Groups"])


# ── Request / Response models ────────────────────────────────


class CreateGroupRequest(BaseModel):
    name: str = Field(description="Unique group name")
    roles: list[str] = Field(description="Roles assigned to all group members")
    description: str = Field("", description="Human-readable description")


class UpdateGroupRequest(BaseModel):
    roles: list[str] | None = Field(None, description="Replace role list")
    description: str | None = Field(None, description="Replace description")


class AddMemberRequest(BaseModel):
    username: str = Field(description="Account username to add")


class GroupResponse(BaseModel):
    name: str
    description: str
    roles: list[str]
    members: list[str]


# ── Endpoints ────────────────────────────────────────────────


@router.post(
    "",
    response_model=GroupResponse,
    status_code=201,
    dependencies=[Depends(require_action("org:write"))],
)
async def create_group(
    body: CreateGroupRequest,
    user: CurrentUser,
    request: Request,
):
    """Create a new RBAC group (admin only)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(body.name) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": f"Group '{body.name}' already exists"},
        )
    group = store.create(body.name, roles=body.roles, description=body.description)
    return GroupResponse(
        name=group.name,
        description=group.description,
        roles=group.roles,
        members=group.members,
    )


@router.get(
    "",
    response_model=list[GroupResponse],
    dependencies=[Depends(require_action("org:write"))],
)
async def list_groups(
    user: CurrentUser,
    request: Request,
):
    """List all groups (admin only)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    return [
        GroupResponse(
            name=g.name,
            description=g.description,
            roles=g.roles,
            members=g.members,
        )
        for g in store.list()
    ]


@router.get(
    "/{name}",
    response_model=GroupResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def get_group(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Get a single group by name (admin only)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    group = store.get(name)
    if group is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    return GroupResponse(
        name=group.name,
        description=group.description,
        roles=group.roles,
        members=group.members,
    )


@router.put(
    "/{name}",
    response_model=GroupResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def update_group(
    name: str,
    body: UpdateGroupRequest,
    user: CurrentUser,
    request: Request,
):
    """Update group roles or description (admin only)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    update_fields: dict[str, object] = {}
    if body.roles is not None:
        update_fields["roles"] = body.roles
    if body.description is not None:
        update_fields["description"] = body.description
    group = store.update(name, **update_fields)
    return GroupResponse(
        name=group.name,
        description=group.description,
        roles=group.roles,
        members=group.members,
    )


@router.post(
    "/{name}/members",
    response_model=GroupResponse,
    status_code=200,
    dependencies=[Depends(require_action("org:write"))],
)
async def add_member(
    name: str,
    body: AddMemberRequest,
    user: CurrentUser,
    request: Request,
):
    """Add a member to a group (admin only, idempotent)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    store.add_member(name, body.username)
    group = store.get(name)
    return GroupResponse(
        name=group.name,
        description=group.description,
        roles=group.roles,
        members=group.members,
    )


@router.delete(
    "/{name}/members/{username}",
    response_model=GroupResponse,
    status_code=200,
    dependencies=[Depends(require_action("org:write"))],
)
async def remove_member(
    name: str,
    username: str,
    user: CurrentUser,
    request: Request,
):
    """Remove a member from a group (admin only)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    store.remove_member(name, username)
    group = store.get(name)
    return GroupResponse(
        name=group.name,
        description=group.description,
        roles=group.roles,
        members=group.members,
    )


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action("org:write"))],
)
async def delete_group(
    name: str,
    user: CurrentUser,
    request: Request,
):
    """Delete a group (admin only)."""
    from dfe_engine.auth.groups import GroupStore

    store: GroupStore = request.app.state.group_store
    if store.get(name) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Group '{name}' not found"},
        )
    store.delete(name)
