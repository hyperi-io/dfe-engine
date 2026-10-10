#  Project:      dfe-engine
#  File:         api/v1/roles.py
#  Purpose:      Role CRUD REST endpoints (admin only)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Role management router -- CRUD for RBAC role definitions.

GET    /api/v1/auth/roles              -> List roles
GET    /api/v1/auth/roles/scopes       -> Casbin permission scopes for role editors
POST   /api/v1/auth/roles              -> Create role
GET    /api/v1/auth/roles/{name}       -> Get role
PUT    /api/v1/auth/roles/{name}       -> Update role
DELETE /api/v1/auth/roles/{name}       -> Delete role

All endpoints require admin role (org:write).
"""

from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search
from dfe_engine.auth.audit import audit_role_change
from dfe_engine.auth.rbac_scopes import casbin_scope_catalog, scopes_dict
from dfe_engine.auth.role_store import Role, RoleStore
from dfe_engine.auth.store_names import VALID_NAME
from dfe_engine.governance.ch import request_ch_rbac_reconcile

RoleResourceTypeQuery = Literal["core", "custom"]

router = APIRouter(prefix="/roles", tags=["Roles"])


class CreateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique role name")
    description: str = Field("", description="Human-readable description")
    permissions: list[str] = Field(description="Casbin-style permission patterns")
    scoped: bool = Field(False, description="When true, role is org-scoped at query time")

    @field_validator("name")
    @classmethod
    def _addressable_name(cls, value: str) -> str:
        """The name is a URL path segment (``/auth/roles/{name}``) and a ``roles.yaml`` key.

        Held to the rule groups, accounts and API keys are, so every role the create
        endpoint accepts is one the get, update and delete routes can reach.
        """
        if not VALID_NAME.match(value):
            raise ValueError(
                "must start with a letter or digit, contain only letters, digits, "
                "dot, underscore or hyphen, and be at most 128 characters"
            )
        return value


class UpdateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(None, description="Replace description")
    permissions: list[str] | None = Field(None, description="Replace permission patterns")
    scoped: bool | None = Field(None, description="Replace org-scoped flag")


class RoleResponse(BaseModel):
    name: str
    description: str
    permissions: list[str]
    scoped: bool
    resource_type: str = Field(description="core for system roles, custom for user-created roles")


class CasbinScopesResponse(BaseModel):
    """Assignable Casbin scopes (paginated) plus picker metadata."""

    scopes: list[str] = Field(description="Permission patterns for the current page")
    total: int = Field(description="Total scopes matching filters")
    page: int = Field(description="Current page (1-based)")
    per_page: int = Field(description="Page size; -1 means all scopes in one page")
    total_pages: int
    next_page: int | None = None
    prev_page: int | None = None
    wildcard: bool
    argo_namespace_prefix: str
    notes: str


def _casbin_scopes_response(
    scope_strings: list[str],
    *,
    page: int,
    per_page: int,
    wildcard: bool,
    argo_namespace_prefix: str,
    notes: str,
) -> CasbinScopesResponse:
    paginated = PaginatedResponse.from_list(scope_strings, page, per_page)
    return CasbinScopesResponse(
        scopes=paginated.items,
        total=paginated.total,
        page=paginated.page,
        per_page=paginated.per_page,
        total_pages=paginated.total_pages,
        next_page=paginated.next_page,
        prev_page=paginated.prev_page,
        wildcard=wildcard,
        argo_namespace_prefix=argo_namespace_prefix,
        notes=notes,
    )


def _role_response(role: Role) -> RoleResponse:
    return RoleResponse(
        name=role.name,
        description=role.description,
        permissions=role.permissions,
        scoped=role.scoped,
        resource_type=role.resource_type,
    )


def _refresh_role_config(request: Request) -> None:
    store: RoleStore = request.app.state.role_store
    request.app.state.role_config = store.load_config()
    # A role's scoped flag decides whether its groups' ClickHouse users are pinned to an org.
    request_ch_rbac_reconcile(request.app.state)


def _grant_details(role: Role) -> dict[str, object]:
    """What a role grants once written, for its audit event."""
    return {"permissions": role.permissions, "scoped": role.scoped}


def _role_in_use(request: Request, role_name: str) -> bool:
    from dfe_engine.auth.groups import GroupStore

    group_store: GroupStore = request.app.state.group_store
    return any(role_name in group.roles for group in group_store.list())


@router.get(
    "/scopes",
    response_model=CasbinScopesResponse,
    dependencies=[Depends(require_action(scopes_dict["role_scopes"]))],
)
async def list_casbin_scopes(
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(
        None,
        description="Case-insensitive substring match on permission scope strings",
    ),
    prefix: str | None = Query(
        None,
        description="Return only scopes that start with this prefix (e.g. config:, argo:)",
    ),
) -> CasbinScopesResponse:
    """Return assignable Casbin permission scopes for role configuration."""
    data = casbin_scope_catalog()
    # catalog is dict[str, object] (heterogeneous values); scopes is a list[str].
    rows = [{"scope": s} for s in cast("list[str]", data["scopes"])]
    if prefix:
        rows = [row for row in rows if str(row["scope"]).startswith(prefix)]
    rows = apply_search(rows, search, ["scope"])
    scope_strings = [str(row["scope"]) for row in rows]
    return _casbin_scopes_response(
        scope_strings,
        page=pagination.page,
        per_page=pagination.per_page,
        wildcard=bool(data["wildcard"]),
        argo_namespace_prefix=str(data["argo_namespace_prefix"]),
        notes=str(data["notes"]),
    )


@router.post(
    "",
    response_model=RoleResponse,
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["role_write"]))],
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
    audit_role_change(user.user_id, role.name, "created", _grant_details(role))
    return _role_response(role)


@router.get(
    "",
    response_model=PaginatedResponse[RoleResponse],
    dependencies=[Depends(require_action(scopes_dict["role_read"]))],
)
async def list_roles(
    user: CurrentUser,
    request: Request,
    pagination: PaginationParams = Depends(),
    resource_type: RoleResourceTypeQuery | None = Query(
        None,
        description="Return only roles with this resource_type (core or custom)",
    ),
    search: str | None = Query(
        None,
        description="Case-insensitive match on role name or description",
    ),
) -> PaginatedResponse[RoleResponse]:
    """List roles with pagination (admin only)."""
    store: RoleStore = request.app.state.role_store
    roles = store.list()
    if resource_type is not None:
        roles = [role for role in roles if role.resource_type == resource_type]

    rows = [_role_response(role).model_dump() for role in roles]
    rows = apply_search(rows, search, ["name", "description"])
    summaries = [RoleResponse.model_validate(row) for row in rows]
    return PaginatedResponse.from_list(summaries, pagination.page, pagination.per_page)


@router.get(
    "/{name}",
    response_model=RoleResponse,
    dependencies=[Depends(require_action(scopes_dict["role_read"]))],
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
    dependencies=[Depends(require_action(scopes_dict["role_write"]))],
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
    audit_role_change(user.user_id, role.name, "updated", _grant_details(role))
    return _role_response(role)


@router.delete(
    "/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["role_delete"]))],
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
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "conflict", "message": str(exc)},
        ) from exc
    except KeyError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Role '{name}' not found"},
        ) from None
    _refresh_role_config(request)
    audit_role_change(user.user_id, name, "deleted")
