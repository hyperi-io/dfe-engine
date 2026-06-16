#  Project:      dfe-engine
#  File:         api/v1/roles.py
#  Purpose:      Role CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Role management router — CRUD for RBAC role definitions.

GET    /api/v1/auth/roles              → List roles
GET    /api/v1/auth/roles/scopes       → Casbin permission scopes for role editors
POST   /api/v1/auth/roles              → Create role
GET    /api/v1/auth/roles/{name}       → Get role
PUT    /api/v1/auth/roles/{name}       → Update role
DELETE /api/v1/auth/roles/{name}       → Delete role

All endpoints require admin role (org:write).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import casbin_scope_catalog
from dfe_engine.auth.role_store import Role, RoleStore

router = APIRouter(prefix="/roles", tags=["Roles"])


class CreateRoleRequest(BaseModel):
    name: str = Field(description="Unique role name")
    description: str = Field("", description="Human-readable description")
    permissions: list[str] = Field(description="Casbin-style permission patterns")
    scoped: bool = Field(False, description="When true, role is org-scoped at query time")


class UpdateRoleRequest(BaseModel):
    description: str | None = Field(None, description="Replace description")
    permissions: list[str] | None = Field(None, description="Replace permission patterns")
    scoped: bool | None = Field(None, description="Replace org-scoped flag")


class RoleResponse(BaseModel):
    name: str
    description: str
    permissions: list[str]
    scoped: bool


class CasbinScopesResponse(BaseModel):
    scopes: list[str]
    wildcard: bool
    argo_namespace_prefix: str
    notes: str


def _role_response(role: Role) -> RoleResponse:
    return RoleResponse(
        name=role.name,
        description=role.description,
        permissions=role.permissions,
        scoped=role.scoped,
    )


def _refresh_role_config(request: Request) -> None:
    store: RoleStore = request.app.state.role_store
    request.app.state.role_config = store.load_config()


def _role_in_use(request: Request, role_name: str) -> bool:
    from dfe_engine.auth.groups import GroupStore

    group_store: GroupStore = request.app.state.group_store
    return any(role_name in group.roles for group in group_store.list())


@router.get(
    "/scopes",
    response_model=CasbinScopesResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def list_casbin_scopes(user: CurrentUser) -> CasbinScopesResponse:
    """Return assignable Casbin permission scopes for role configuration."""
    data = casbin_scope_catalog()
    return CasbinScopesResponse.model_validate(data)


@router.post(
    "",
    response_model=RoleResponse,
    status_code=201,
    dependencies=[Depends(require_action("org:write"))],
)
async def create_role(
    body: CreateRoleRequest,
    user: CurrentUser,
    request: Request,
) -> RoleResponse:
    """Create a new RBAC role (admin only)."""
    store: RoleStore = request.app.state.role_store
    try:
        role = store.create(
            body.name,
            description=body.description,
            permissions=body.permissions,
            scoped=body.scoped,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    _refresh_role_config(request)
    return _role_response(role)


@router.get(
    "",
    response_model=list[RoleResponse],
    dependencies=[Depends(require_action("org:write"))],
)
async def list_roles(
    user: CurrentUser,
    request: Request,
) -> list[RoleResponse]:
    """List all roles (admin only)."""
    store: RoleStore = request.app.state.role_store
    return [_role_response(role) for role in store.list()]


@router.get(
    "/{name}",
    response_model=RoleResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def get_role(
    name: str,
    user: CurrentUser,
    request: Request,
) -> RoleResponse:
    """Get one role by name (admin only)."""
    store: RoleStore = request.app.state.role_store
    role = store.get(name)
    if role is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Role '{name}' not found"},
        )
    return _role_response(role)


@router.put(
    "/{name}",
    response_model=RoleResponse,
    dependencies=[Depends(require_action("org:write"))],
)
async def update_role(
    name: str,
    body: UpdateRoleRequest,
    user: CurrentUser,
    request: Request,
) -> RoleResponse:
    """Update a role (admin only)."""
    store: RoleStore = request.app.state.role_store
    try:
        role = store.update(
            name,
            description=body.description,
            permissions=body.permissions,
            scoped=body.scoped,
        )
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Role '{name}' not found"},
        ) from None
    _refresh_role_config(request)
    return _role_response(role)


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action("org:write"))],
)
async def delete_role(
    name: str,
    user: CurrentUser,
    request: Request,
) -> None:
    """Delete a role (admin only)."""
    if _role_in_use(request, name):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Role '{name}' is assigned to one or more groups",
            },
        )
    store: RoleStore = request.app.state.role_store
    try:
        store.delete(name)
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Role '{name}' not found"},
        ) from None
    _refresh_role_config(request)
