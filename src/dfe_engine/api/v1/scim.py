#  Project:      dfe-engine
#  File:         api/v1/scim.py
#  Purpose:      SCIM 2.0 User/Group provisioning face over the account/group stores
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""SCIM 2.0 provisioning endpoints (RFC 7643/7644).

External IdPs (Okta, Entra) push users and groups into DFE here. This is an
ADDITIVE face over the SAME gitops-backed
:class:`~dfe_engine.auth.accounts.AccountStore` /
:class:`~dfe_engine.auth.groups.GroupStore` used by the native
``/auth/accounts`` and ``/auth/groups`` routers - a SCIM ``User`` IS an
``Account``, a SCIM ``Group`` IS a ``Group``.

Mounted under ``/api/v1/scim/v2``::

    GET|POST            /Users
    GET|PUT|PATCH|DELETE /Users/{id}
    GET|POST            /Groups
    GET|PUT|PATCH|DELETE /Groups/{id}
    GET  /ServiceProviderConfig
    GET  /ResourceTypes
    GET  /Schemas

Resource ``id`` == the store key (username / group name). Writes are gated
behind the same RBAC actions as the native routers; discovery endpoints are
open (IdPs probe them before presenting a token). Responses use the SCIM
media type and the SCIM error envelope.
"""

import re

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from scim2_models import (
    AuthenticationScheme,
    Bulk,
    ChangePassword,
    Context,
    Error,
    ETag,
    Filter,
    ListResponse,
    Patch,
    PatchOp,
    ResourceType,
    Schema,
    ServiceProviderConfig,
    Sort,
)
from scim2_models import (
    Group as ScimGroup,
)
from scim2_models import (
    User as ScimUser,
)

from dfe_engine.api.deps import CurrentUser, provider_bindings, require_action
from dfe_engine.api.password_floor import FLOOR_MESSAGE, below_floor, count_floor_refusal
from dfe_engine.api.v1.account_groups import check_group_changes, check_role_assignment, scope_of
from dfe_engine.auth.accounts import AccountExistsError, discard_created
from dfe_engine.auth.audit import audit_account_change, audit_group_change
from dfe_engine.auth.groups import (
    Group,
    GroupExistsError,
    identifier_holder,
    source_id_holder,
)
from dfe_engine.auth.membership import forget_member, groups_held, linked_providers
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.scim_mapping import (
    account_to_scim_user,
    generate_provisioning_password,
    group_to_scim_group,
    scim_group_to_group_fields,
    scim_user_to_account_fields,
)
from dfe_engine.governance.ch import request_ch_rbac_reconcile
from dfe_engine.names import InvalidNameError, validate_store_name

router = APIRouter(prefix="/scim/v2", tags=["SCIM"])

SCIM_MEDIA_TYPE = "application/scim+json"

# Where this router is mounted (v1_router "/v1" under app "/api"). Used to build
# absolute meta.location URLs, and by the error handlers to tell a SCIM request
# from a native one. Kept as a constant so a resource's self-link is stable;
# adjust here if the mount point ever moves.
SCIM_ROOT = "/api/v1/scim/v2"

# members[value eq "alice"] -> "alice" (the value-path filter IdPs use on remove)
_MEMBER_FILTER_RE = re.compile(r'value\s+eq\s+"([^"]+)"', re.IGNORECASE)
# userName eq "alice" / displayName eq "Admins" (the only filter form we honour)
_EQ_FILTER_RE = re.compile(r'^\s*(\w+)\s+eq\s+"([^"]+)"\s*$', re.IGNORECASE)


# -- Response / error helpers ---------------------------------


def _location(request: Request, kind: str, rid: str) -> str:
    """Absolute self-link for a SCIM resource (``meta.location``)."""
    return f"{str(request.base_url).rstrip('/')}{SCIM_ROOT}/{kind}/{rid}"


def _scim_json(model, status_code: int, request: Request | None = None) -> JSONResponse:
    """Serialise a SCIM model in a response context and return scim+json."""
    payload = model.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE)
    headers = {}
    if request is not None and getattr(model, "meta", None) and model.meta.location:
        headers["Location"] = model.meta.location
    return JSONResponse(
        content=payload,
        status_code=status_code,
        media_type=SCIM_MEDIA_TYPE,
        headers=headers,
    )


def scim_error(status_code: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    """Return an RFC 7644 SCIM error envelope.

    Public because the app-level exception handlers answer a SCIM request in this
    envelope rather than the native one.
    """
    err = Error(status=status_code, detail=detail, scim_type=scim_type)
    return JSONResponse(
        content=err.model_dump(),
        status_code=status_code,
        media_type=SCIM_MEDIA_TYPE,
    )


def _user_exists(username: str) -> JSONResponse:
    """The 409 a create answers when an account already holds *username*."""
    return scim_error(409, f"User '{username}' already exists", "uniqueness")


def _invalid_body(kind: str, exc: Exception) -> JSONResponse:
    """Answer a body that failed validation, naming what failed but no value it carried.

    ``str()`` of a pydantic ``ValidationError`` repeats the input, which for a
    model-level error is the whole request, password included. The detail is built
    from the error locations and messages only. Anything else scim2_models raises on
    a malformed body is its own internal failure, so it gets a fixed message.
    """
    if not isinstance(exc, ValidationError):
        return scim_error(
            400, f"Invalid {kind}: the body does not match the {kind} schema", "invalidValue"
        )
    problems = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        where = ".".join(str(part) for part in err["loc"])
        problems.append(f"{where}: {err['msg']}" if where else err["msg"])
    return scim_error(400, f"Invalid {kind}: {'; '.join(problems)}", "invalidValue")


async def _parse_body(request: Request):
    """Read and JSON-decode the request body, or None on malformed JSON."""
    try:
        return await request.json()
    except Exception:
        return None


def _parse_eq_filter(raw: str | None) -> tuple[str, str] | None:
    """Parse a ``<attr> eq "<value>"`` filter into ``(attr, value)``."""
    if not raw:
        return None
    m = _EQ_FILTER_RE.match(raw)
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def _page_params(request: Request) -> tuple[int, int]:
    """Return 1-based ``(start_index, count)`` from query params (SCIM defaults)."""
    try:
        start_index = max(1, int(request.query_params.get("startIndex", "1")))
    except ValueError:
        start_index = 1
    count_raw = request.query_params.get("count")
    try:
        count = int(count_raw) if count_raw is not None else 100
    except ValueError:
        count = 100
    return start_index, max(0, count)


def _audit_details(**fields) -> dict:
    """Audit-event details for a SCIM write, marked so an IdP push reads apart from an admin's."""
    return {"via": "scim", **fields}


def _refuse_identifier_collision(
    *, name: str, source_id: str, store, target: str
) -> JSONResponse | None:
    """Refuse a displayName that is another group's provider id or an externalId that is another group's name (:func:`identifier_holder`); pass empty for the one the write leaves alone."""
    holder = identifier_holder(exclude=target, groups=store.list(), name=name, source_id=source_id)
    if holder is None:
        return None
    if name and holder.source_id == name:
        detail = f"displayName {name!r} is already another group's provider id"
    else:
        detail = f"externalId {source_id!r} is already another group's name"
    return scim_error(detail=detail, scim_type="uniqueness", status_code=409)


def _refuse_taken_provider_id(store, source_id: str, name: str) -> JSONResponse | None:
    """Refuse an externalId another group already carries (:func:`source_id_holder`)."""
    if source_id_holder(groups=store.list(), name=name, source_id=source_id) is None:
        return None
    return scim_error(
        409, f"externalId '{source_id}' is already another group's provider id", "uniqueness"
    )


def _scim_may_move_link(group: Group, stamp: str, request: Request) -> bool:
    """Whether a SCIM write stamped *stamp* may set, change or clear *group*'s provider id.

    An unlinked group takes SCIM's stamp with the id. A linked one moves only when its
    provider is SCIM's own stamp or one ``auth.source_provider_bindings`` joins to it,
    the providers a SCIM account's membership resolves through
    (:func:`~dfe_engine.auth.membership.linked_providers`).
    """
    if not group.source_provider:
        return True
    return group.source_provider in linked_providers([stamp], provider_bindings(request))


def _scim_last_admin_member_removals(
    request: Request, *, group_name: str, removed: set[str] | list[str]
) -> JSONResponse | None:
    """Refuse a membership write that would leave no usable system admin."""
    from dfe_engine.auth import admin_retirement

    dropped = sorted({name for name in removed if name})
    if not dropped:
        return None
    if not admin_retirement.locks_out_last_admin_by_member_removals(
        request.app.state.account_store,
        request.app.state.group_store,
        bindings=provider_bindings(request),
        group_name=group_name,
        removed=dropped,
    ):
        return None
    return scim_error(
        409,
        f"Cannot remove the last usable system admin from '{group_name}'",
        "mutability",
    )


# -- /Users ---------------------------------------------------


@router.get(
    "/Users",
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
)
async def list_users(user: CurrentUser, request: Request) -> Response:
    """List users (SCIM ListResponse). Supports ``userName eq`` filter + paging."""
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    accounts = store.list()

    filt = _parse_eq_filter(request.query_params.get("filter"))
    if filt and filt[0] == "username":
        accounts = [a for a in accounts if a.username == filt[1]]

    start_index, count = _page_params(request)
    total = len(accounts)
    window = accounts[start_index - 1 : start_index - 1 + count] if count else []

    groups = group_store.list()
    bindings = provider_bindings(request)
    resources = [
        account_to_scim_user(
            a,
            groups=groups_held(a, groups, bindings=bindings),
            location=_location(request, "Users", a.username),
        )
        for a in window
    ]
    lr = ListResponse[ScimUser](
        total_results=total,
        start_index=start_index,
        items_per_page=len(resources),
        resources=resources or None,
    )
    return JSONResponse(
        content=lr.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        media_type=SCIM_MEDIA_TYPE,
    )


@router.get(
    "/Users/{user_id}",
    dependencies=[Depends(require_action(scopes_dict["account_read"]))],
)
async def get_user(user_id: str, user: CurrentUser, request: Request) -> Response:
    """Fetch a single user by id (username)."""
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    account = store.get(user_id)
    if account is None:
        return scim_error(404, f"User '{user_id}' not found")
    groups = groups_held(account, group_store.list(), bindings=provider_bindings(request))
    scim_user = account_to_scim_user(
        account, groups=groups, location=_location(request, "Users", user_id)
    )
    return _scim_json(scim_user, 200, request)


@router.post(
    "/Users",
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def create_user(user: CurrentUser, request: Request) -> Response:
    """Provision a user. IdP-owned; local password is randomised when omitted.

    A password the IdP does send is held to the same length floor as every other
    password the API sets, and one under it is refused rather than replaced.
    """
    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    try:
        inbound = ScimUser.model_validate(body, scim_ctx=Context.RESOURCE_CREATION_REQUEST)
    except Exception as exc:
        return _invalid_body("User", exc)
    if not inbound.user_name:
        return scim_error(400, "userName is required", "invalidValue")

    store = request.app.state.account_store
    if store.get(inbound.user_name) is not None:
        return _user_exists(inbound.user_name)

    fields = scim_user_to_account_fields(inbound)
    username = str(fields["username"])
    password = str(fields.pop("password", generate_provisioning_password()))
    if below_floor(password):
        count_floor_refusal(request)
        return scim_error(400, FLOOR_MESSAGE, "invalidValue")
    # The create decides: another replica can take the name after the lookup above.
    try:
        created = store.create(username, password, groups=[])
    except AccountExistsError:
        return _user_exists(username)
    except ValueError as exc:
        # A userName no account can have, or a password the hash refuses.
        return scim_error(400, str(exc), "invalidValue")
    # Apply the remaining writable attributes (enabled, external_id, provider).
    try:
        store.update(
            username,
            enabled=fields["enabled"],
            external_id=fields["external_id"],
            source_provider=fields["source_provider"],
        )
    except Exception:
        # Left unstamped, the account reads as a local one and the IdP's retry meets a 409.
        discard_created(store, created)
        raise
    audit_account_change(
        user.user_id, username, "created", _audit_details(enabled=bool(fields["enabled"]))
    )
    account = store.get(username)
    scim_user = account_to_scim_user(
        account, groups=[], location=_location(request, "Users", username)
    )
    return _scim_json(scim_user, 201, request)


@router.put(
    "/Users/{user_id}",
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def replace_user(user_id: str, user: CurrentUser, request: Request) -> Response:
    """Replace a user's writable attributes (SCIM PUT)."""
    from dfe_engine.auth import admin_retirement

    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = store.get(user_id)
    if existing is None:
        return scim_error(404, f"User '{user_id}' not found")
    try:
        inbound = ScimUser.model_validate(body, scim_ctx=Context.RESOURCE_REPLACEMENT_REQUEST)
    except Exception as exc:
        return _invalid_body("User", exc)

    fields = scim_user_to_account_fields(inbound)
    if user_id == user.user_id and not fields["enabled"]:
        return scim_error(403, "Cannot deactivate your own account")
    if admin_retirement.locks_out_last_admin(
        existing,
        store,
        group_store,
        bindings=provider_bindings(request),
        enabled=bool(fields["enabled"]),
    ):
        return scim_error(
            409,
            (
                f"Cannot deactivate '{user_id}': it is the last usable account "
                "with the system admin role"
            ),
        )
    store.update(
        user_id,
        enabled=fields["enabled"],
        external_id=fields["external_id"],
    )
    details = _audit_details(fields=["enabled", "external_id"], enabled=bool(fields["enabled"]))
    audit_account_change(user.user_id, user_id, "updated", details)
    account = store.get(user_id)
    groups = groups_held(account, group_store.list(), bindings=provider_bindings(request))
    scim_user = account_to_scim_user(
        account, groups=groups, location=_location(request, "Users", user_id)
    )
    return _scim_json(scim_user, 200, request)


@router.patch(
    "/Users/{user_id}",
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def patch_user(user_id: str, user: CurrentUser, request: Request) -> Response:
    """Apply a PatchOp - primarily the ``active`` toggle IdPs use to deprovision."""
    from dfe_engine.auth import admin_retirement

    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = store.get(user_id)
    if existing is None:
        return scim_error(404, f"User '{user_id}' not found")
    try:
        patch = PatchOp[ScimUser].model_validate(body, scim_ctx=Context.RESOURCE_PATCH_REQUEST)
    except Exception as exc:
        return _invalid_body("PatchOp", exc)

    enabled: bool | None = None
    for op in patch.operations or []:
        # op.path may be a typed ``Path`` object; coerce to a plain string.
        path = str(op.path or "").lower()
        value = op.value
        if path == "active":
            enabled = bool(value)
        elif path == "" and isinstance(value, dict) and "active" in value:
            enabled = bool(value["active"])
        # Other paths (userName is the immutable key; group membership is managed
        # on /Groups) are ignored - the record stays consistent either way.
    if enabled is not None:
        if user_id == user.user_id and not enabled:
            return scim_error(403, "Cannot deactivate your own account")
        if admin_retirement.locks_out_last_admin(
            existing,
            store,
            group_store,
            bindings=provider_bindings(request),
            enabled=enabled,
        ):
            return scim_error(
                409,
                (
                    f"Cannot deactivate '{user_id}': it is the last usable account "
                    "with the system admin role"
                ),
            )
        store.update(user_id, enabled=enabled)
        details = _audit_details(fields=["enabled"], enabled=enabled)
        audit_account_change(user.user_id, user_id, "updated", details)

    account = store.get(user_id)
    groups = groups_held(account, group_store.list(), bindings=provider_bindings(request))
    scim_user = account_to_scim_user(
        account, groups=groups, location=_location(request, "Users", user_id)
    )
    return _scim_json(scim_user, 200, request)


@router.delete(
    "/Users/{user_id}",
    dependencies=[Depends(require_action(scopes_dict["account_write"]))],
)
async def delete_user(user_id: str, user: CurrentUser, request: Request) -> Response:
    """Delete a user (SCIM 204)."""
    from dfe_engine.auth import admin_retirement

    store = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = store.get(user_id)
    if existing is None:
        return scim_error(404, f"User '{user_id}' not found")
    if user_id == user.user_id:
        return scim_error(403, "Cannot delete your own account")
    # Deleting a user takes it out of every group it holds, so it needs their roles.
    groups = group_store.list()
    bindings = provider_bindings(request)
    held = groups_held(existing, groups, bindings=bindings)
    check_group_changes(after=(), before=held, groups=groups, request=request, user=user)
    if admin_retirement.account_holds_system_admin(existing, groups, bindings=bindings):
        if not admin_retirement.other_usable_admin_exists(
            store, group_store, bindings=bindings, excluding=user_id
        ):
            return scim_error(
                409,
                (
                    f"Cannot delete '{user_id}': it is the last usable account "
                    "with the system admin role"
                ),
            )
    store.delete(user_id)
    forget_member(group_store, user_id)
    audit_account_change(user.user_id, user_id, "deleted", _audit_details(groups=held))
    return Response(status_code=204)


# -- /Groups --------------------------------------------------


@router.get(
    "/Groups",
    dependencies=[Depends(require_action(scopes_dict["group_read"]))],
)
async def list_groups(user: CurrentUser, request: Request) -> Response:
    """List groups (SCIM ListResponse). Supports ``displayName eq`` filter + paging."""
    store = request.app.state.group_store
    groups = store.list()

    filt = _parse_eq_filter(request.query_params.get("filter"))
    if filt and filt[0] == "displayname":
        groups = [g for g in groups if g.name == filt[1]]

    start_index, count = _page_params(request)
    total = len(groups)
    window = groups[start_index - 1 : start_index - 1 + count] if count else []

    resources = [
        group_to_scim_group(g, location=_location(request, "Groups", g.name)) for g in window
    ]
    lr = ListResponse[ScimGroup](
        total_results=total,
        start_index=start_index,
        items_per_page=len(resources),
        resources=resources or None,
    )
    return JSONResponse(
        content=lr.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        media_type=SCIM_MEDIA_TYPE,
    )


@router.get(
    "/Groups/{group_id}",
    dependencies=[Depends(require_action(scopes_dict["group_read"]))],
)
async def get_group(group_id: str, user: CurrentUser, request: Request) -> Response:
    """Fetch a single group by id (name)."""
    store = request.app.state.group_store
    group = store.get(group_id)
    if group is None:
        return scim_error(404, f"Group '{group_id}' not found")
    return _scim_json(
        group_to_scim_group(group, location=_location(request, "Groups", group_id)), 200, request
    )


@router.post(
    "/Groups",
    dependencies=[Depends(require_action(scopes_dict["group_write"]))],
)
async def create_group(user: CurrentUser, request: Request) -> Response:
    """Provision a group with its member set (member Account.groups kept in sync)."""
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    try:
        inbound = ScimGroup.model_validate(body, scim_ctx=Context.RESOURCE_CREATION_REQUEST)
    except Exception as exc:
        return _invalid_body("Group", exc)
    if not inbound.display_name:
        return scim_error(400, "displayName is required", "invalidValue")

    store = request.app.state.group_store
    account_store = request.app.state.account_store
    fields = scim_group_to_group_fields(inbound)
    name = str(fields["name"])
    try:
        validate_store_name(name)
    except InvalidNameError as exc:
        return scim_error(400, f"displayName: {exc}", "invalidValue")
    members = list(fields["members"])  # type: ignore[arg-type]
    taken = _refuse_taken_provider_id(store, str(fields["source_id"]), name)
    if taken is not None:
        return taken
    collision = _refuse_identifier_collision(
        name=name, source_id=str(fields["source_id"]), store=store, target=name
    )
    if collision is not None:
        return collision
    # create() decides, not get(): get() reports a stored group that does not load as absent.
    try:
        group = store.create(name, roles=[], description="", members=members)
    except GroupExistsError:
        return scim_error(409, f"Group '{name}' already exists", "uniqueness")
    store.update(name, source_id=fields["source_id"], source_provider=fields["source_provider"])
    request_ch_rbac_reconcile(request.app.state)
    sync_account_groups_for_membership_change(account_store, name, added=group.members)
    group = store.get(name)
    details = _audit_details(roles=group.roles, scope=group.scope, members=group.members)
    audit_group_change(user.user_id, name, "created", details)
    return _scim_json(
        group_to_scim_group(group, location=_location(request, "Groups", name)), 201, request
    )


@router.put(
    "/Groups/{group_id}",
    dependencies=[Depends(require_action(scopes_dict["group_write"]))],
)
async def replace_group(group_id: str, user: CurrentUser, request: Request) -> Response:
    """Replace a group's member set (SCIM PUT)."""
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    store = request.app.state.group_store
    account_store = request.app.state.account_store
    existing = store.get(group_id)
    if existing is None:
        return scim_error(404, f"Group '{group_id}' not found")
    try:
        inbound = ScimGroup.model_validate(body, scim_ctx=Context.RESOURCE_REPLACEMENT_REQUEST)
    except Exception as exc:
        return _invalid_body("Group", exc)

    fields = scim_group_to_group_fields(inbound)
    new_members = list(fields["members"])  # type: ignore[arg-type]
    old = set(existing.members)
    new = set(new_members)
    moves_link = fields["source_id"] != existing.source_id
    # An IdP login resolves a group by its provider id, so moving the id moves the roles.
    if old != new or moves_link:
        check_role_assignment(request, user, existing.roles, scope_of(existing))
    last_admin = _scim_last_admin_member_removals(request, group_name=group_id, removed=old - new)
    if last_admin is not None:
        return last_admin
    if moves_link:
        if not _scim_may_move_link(existing, str(fields["source_provider"]), request):
            return scim_error(
                400,
                "externalId cannot change: the group is linked through an identity "
                "provider SCIM is not bound to",
                "mutability",
            )
        taken = _refuse_taken_provider_id(store, str(fields["source_id"]), group_id)
        if taken is not None:
            return taken
        collision = _refuse_identifier_collision(
            name="", source_id=str(fields["source_id"]), store=store, target=group_id
        )
        if collision is not None:
            return collision
    # A link names its provider: an id set on an unlinked group takes SCIM's, a linked one keeps its own.
    provider = existing.source_provider
    if fields["source_id"] and not provider:
        provider = fields["source_provider"]
    store.update(
        group_id,
        members=new_members,
        source_id=fields["source_id"],
        source_provider=provider,
    )
    sync_account_groups_for_membership_change(
        account_store, group_id, added=new - old, removed=old - new
    )
    details = _audit_details(
        fields=["members", "source_id"], added=sorted(new - old), removed=sorted(old - new)
    )
    audit_group_change(user.user_id, group_id, "updated", details)
    group = store.get(group_id)
    return _scim_json(
        group_to_scim_group(group, location=_location(request, "Groups", group_id)), 200, request
    )


@router.patch(
    "/Groups/{group_id}",
    dependencies=[Depends(require_action(scopes_dict["group_write"]))],
)
async def patch_group(group_id: str, user: CurrentUser, request: Request) -> Response:
    """Apply a PatchOp on group membership (add/remove/replace members)."""
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    store = request.app.state.group_store
    account_store = request.app.state.account_store
    group = store.get(group_id)
    if group is None:
        return scim_error(404, f"Group '{group_id}' not found")
    try:
        patch = PatchOp[ScimGroup].model_validate(body, scim_ctx=Context.RESOURCE_PATCH_REQUEST)
    except Exception as exc:
        return _invalid_body("PatchOp", exc)

    try:
        members = _patched_members(patch, group.members)
    except _NoTargetError as exc:
        return scim_error(400, str(exc), "noTarget")
    added = set(members) - set(group.members)
    removed = set(group.members) - set(members)

    # Checked before anything is written, so a refusal leaves the membership as it was.
    if added or removed:
        check_role_assignment(request, user, group.roles, scope_of(group))
        last_admin = _scim_last_admin_member_removals(request, group_name=group_id, removed=removed)
        if last_admin is not None:
            return last_admin
        # One write for the whole set; the store refuses it whole if it drops a recovery credential.
        store.update(group_id, members=members)
        sync_account_groups_for_membership_change(
            account_store, group_id, added=added, removed=removed
        )
        details = _audit_details(fields=["members"], added=sorted(added), removed=sorted(removed))
        audit_group_change(user.user_id, group_id, "updated", details)
    group = store.get(group_id)
    return _scim_json(
        group_to_scim_group(group, location=_location(request, "Groups", group_id)), 200, request
    )


@router.delete(
    "/Groups/{group_id}",
    dependencies=[Depends(require_action(scopes_dict["group_delete"]))],
)
async def delete_group(group_id: str, user: CurrentUser, request: Request) -> Response:
    """Delete a group. Members are detached first so the store permits removal."""
    from dfe_engine.auth.membership import sync_account_groups_for_membership_change

    store = request.app.state.group_store
    account_store = request.app.state.account_store
    group = store.get(group_id)
    if group is None:
        return scim_error(404, f"Group '{group_id}' not found")
    # GroupStore.delete refuses a non-empty group; detach members first and mirror
    # the removal onto each Account.groups.
    members = list(group.members)
    if (members) or (group.source_id):
        # Its members and every login its source ID answers lose the roles it carries.
        check_role_assignment(request, user, group.roles, scope_of(group))
    # Checked before the first detach, so deleting the admin group with a recovery
    # credential in it refuses whole rather than part-way through.
    store.protected.check_member_removal(group_id, members)
    last_admin = _scim_last_admin_member_removals(request, group_name=group_id, removed=members)
    if last_admin is not None:
        return last_admin
    for username in members:
        store.remove_member(group_id, username)
    sync_account_groups_for_membership_change(account_store, group_id, removed=members)
    store.delete(group_id)
    request_ch_rbac_reconcile(request.app.state)
    details = _audit_details(
        members=members, source_id=group.source_id, source_provider=group.source_provider
    )
    audit_group_change(
        admin_id=user.user_id, change="deleted", details=details, group_name=group_id
    )
    return Response(status_code=204)


def _op_kind(op) -> str:
    """The lowercase verb of a PatchOp operation (add/remove/replace)."""
    return getattr(op.op, "value", str(op.op)).lower()


class _NoTargetError(ValueError):
    """A ``replace`` whose value filter matched no current member (RFC 7644 section 3.5.2.3)."""


def _targets_members(op) -> bool:
    """Whether a group PatchOp operation acts on ``members``."""
    # op.path may be a typed ``Path`` object; coerce to a plain string.
    path = str(op.path or "")
    if path:
        return path.lower().startswith("members")
    # A path-less add or replace names the attributes it sets in its value.
    value = op.value
    return _op_kind(op) in ("add", "replace") and isinstance(value, dict) and "members" in value


def _appended(members: list[str], usernames: list[str]) -> list[str]:
    """*members* with each of *usernames* not already in it appended, in order."""
    result = list(members)
    for username in usernames:
        if username not in result:
            result.append(username)
    return result


def _patched_members(patch, members: list[str]) -> list[str]:
    """The member list a group PatchOp leaves, worked out before anything is written.

    Per RFC 7644 section 3.5.2: ``add`` joins members, ``replace`` swaps the whole
    set (or, under a ``members[value eq "x"]`` filter, only the member it names),
    and ``remove`` drops the named members, or every member when it names none.

    Raises:
        _NoTargetError: A filtered ``replace`` names a user who is not a member.
    """
    result = list(members)
    for op in patch.operations or []:
        if not _targets_members(op):
            continue
        kind = _op_kind(op)
        path = str(op.path or "")
        match = _MEMBER_FILTER_RE.search(path)
        values = _member_values(op.value)
        if kind == "add":
            result = _appended(result, values)
        elif kind == "replace":
            if match is None:
                result = _appended([], values)
                continue
            target = match.group(1)
            if target not in result:
                raise _NoTargetError(f"'{target}' is not a member of this group")
            result = _appended([m for m in result if m != target], values)
        elif kind == "remove":
            targets = [match.group(1)] if match else values
            result = [m for m in result if m not in targets] if targets else []
    return result


def _member_values(value) -> list[str]:
    """Extract member usernames from a PatchOperation value payload."""
    if value is None:
        return []
    if isinstance(value, dict):
        # {"members": [{"value": "alice"}, ...]} or a single {"value": "alice"}
        if "value" in value:
            return [str(value["value"])]
        inner = value.get("members")
        if isinstance(inner, list):
            return [str(m["value"]) for m in inner if isinstance(m, dict) and "value" in m]
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict) and "value" in item:
                out.append(str(item["value"]))
            elif isinstance(item, str):
                out.append(item)
        return out
    if isinstance(value, str):
        return [value]
    return []


# -- Discovery (open - IdPs probe these before presenting a token) --


@router.get("/ServiceProviderConfig")
async def service_provider_config(request: Request) -> Response:
    """Static SCIM capability advertisement (RFC 7643 s5)."""
    config = ServiceProviderConfig(
        documentation_uri=None,
        patch=Patch(supported=True),
        bulk=Bulk(supported=False, max_operations=0, max_payload_size=0),
        filter=Filter(supported=True, max_results=200),
        change_password=ChangePassword(supported=False),
        sort=Sort(supported=False),
        etag=ETag(supported=False),
        authentication_schemes=[
            AuthenticationScheme(
                type="oauthbearertoken",
                name="OAuth Bearer Token",
                description="Authentication via the DFE bearer token.",
                primary=True,
            )
        ],
    )
    config.meta = None
    return JSONResponse(
        content=config.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        media_type=SCIM_MEDIA_TYPE,
    )


@router.get("/ResourceTypes")
async def resource_types(request: Request) -> Response:
    """Advertise the User and Group resource types."""
    types = [ResourceType.from_resource(ScimUser), ResourceType.from_resource(ScimGroup)]
    lr = ListResponse[ResourceType](
        total_results=len(types), start_index=1, items_per_page=len(types), resources=types
    )
    return JSONResponse(
        content=lr.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        media_type=SCIM_MEDIA_TYPE,
    )


@router.get("/Schemas")
async def schemas(request: Request) -> Response:
    """Advertise the User and Group core schemas (generated from scim2-models)."""
    defs: list[Schema] = [ScimUser.to_schema(), ScimGroup.to_schema()]
    lr = ListResponse[Schema](
        total_results=len(defs), start_index=1, items_per_page=len(defs), resources=defs
    )
    return JSONResponse(
        content=lr.model_dump(scim_ctx=Context.RESOURCE_QUERY_RESPONSE),
        media_type=SCIM_MEDIA_TYPE,
    )
