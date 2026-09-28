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

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from scalo.logger import logger
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

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.api.password_floor import FLOOR_MESSAGE, below_floor, count_floor_refusal
from dfe_engine.api.v1.account_groups import check_group_changes, check_role_assignment, scope_of
from dfe_engine.auth.groups import GroupExistsError
from dfe_engine.auth.membership import forget_member, groups_held
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.auth.scim_mapping import (
    account_to_scim_user,
    generate_provisioning_password,
    group_to_scim_group,
    scim_group_to_group_fields,
    scim_user_to_account_fields,
)
from dfe_engine.governance.ch import request_ch_rbac_reconcile

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


def _refuse_taken_provider_id(store, source_id: str, name: str) -> JSONResponse | None:
    """Refuse an externalId another group already carries as its provider id.

    Login resolves a provider id to one group, and a second group sharing it would
    decide by name order whose roles every IdP user asserting it gets.
    """
    if not source_id:
        return None
    if any(group.source_id == source_id and group.name != name for group in store.list()):
        return scim_error(
            409, f"externalId '{source_id}' is already another group's provider id", "uniqueness"
        )
    return None


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
    resources = [
        account_to_scim_user(
            a,
            groups=groups_held(a, groups),
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
    groups = groups_held(account, group_store.list())
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
        return scim_error(409, f"User '{inbound.user_name}' already exists", "uniqueness")

    fields = scim_user_to_account_fields(inbound)
    username = str(fields["username"])
    password = str(fields.pop("password", generate_provisioning_password()))
    if below_floor(password):
        count_floor_refusal(request)
        return scim_error(400, FLOOR_MESSAGE, "invalidValue")
    store.create(username, password, groups=[])
    # Apply the remaining writable attributes (enabled, external_id, provider).
    store.update(
        username,
        enabled=fields["enabled"],
        external_id=fields["external_id"],
        source_provider=fields["source_provider"],
    )
    logger.info("SCIM user provisioned", username=username)
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
    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    if store.get(user_id) is None:
        return scim_error(404, f"User '{user_id}' not found")
    try:
        inbound = ScimUser.model_validate(body, scim_ctx=Context.RESOURCE_REPLACEMENT_REQUEST)
    except Exception as exc:
        return _invalid_body("User", exc)

    fields = scim_user_to_account_fields(inbound)
    store.update(
        user_id,
        enabled=fields["enabled"],
        external_id=fields["external_id"],
    )
    account = store.get(user_id)
    groups = groups_held(account, group_store.list())
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
    body = await _parse_body(request)
    if body is None:
        return scim_error(400, "Malformed JSON body", "invalidSyntax")
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    if store.get(user_id) is None:
        return scim_error(404, f"User '{user_id}' not found")
    try:
        patch = PatchOp[ScimUser].model_validate(body, scim_ctx=Context.RESOURCE_PATCH_REQUEST)
    except Exception as exc:
        return _invalid_body("PatchOp", exc)

    for op in patch.operations or []:
        # op.path may be a typed ``Path`` object; coerce to a plain string.
        path = str(op.path or "").lower()
        value = op.value
        if path == "active":
            store.update(user_id, enabled=bool(value))
        elif path == "" and isinstance(value, dict) and "active" in value:
            store.update(user_id, enabled=bool(value["active"]))
        # Other paths (userName is the immutable key; group membership is managed
        # on /Groups) are ignored - the record stays consistent either way.

    account = store.get(user_id)
    groups = groups_held(account, group_store.list())
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
    store = request.app.state.account_store
    group_store = request.app.state.group_store
    existing = store.get(user_id)
    if existing is None:
        return scim_error(404, f"User '{user_id}' not found")
    # Deleting a user takes it out of every group it holds, so it needs their roles.
    groups = group_store.list()
    check_group_changes(request, user, groups, groups_held(existing, groups), ())
    store.delete(user_id)
    forget_member(group_store, user_id)
    logger.info("SCIM user deleted", username=user_id)
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
    members = list(fields["members"])  # type: ignore[arg-type]
    taken = _refuse_taken_provider_id(store, str(fields["source_id"]), name)
    if taken is not None:
        return taken
    # create() decides, not get(): get() reports a stored group that does not load as absent.
    try:
        group = store.create(name, roles=[], description="", members=members)
    except GroupExistsError:
        return scim_error(409, f"Group '{name}' already exists", "uniqueness")
    store.update(name, source_id=fields["source_id"], source_provider=fields["source_provider"])
    request_ch_rbac_reconcile(request.app.state)
    sync_account_groups_for_membership_change(account_store, name, added=group.members)
    logger.info("SCIM group provisioned", group=name, members=len(group.members))
    group = store.get(name)
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
    # An IdP login resolves a group by its provider id, so moving the id moves the roles.
    if old != new or fields["source_id"] != existing.source_id:
        check_role_assignment(request, user, existing.roles, scope_of(existing))
    if fields["source_id"] != existing.source_id:
        taken = _refuse_taken_provider_id(store, str(fields["source_id"]), group_id)
        if taken is not None:
            return taken
    store.update(
        group_id,
        members=new_members,
        source_id=fields["source_id"],
    )
    sync_account_groups_for_membership_change(
        account_store, group_id, added=new - old, removed=old - new
    )
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

    # Checked before any op applies, so a refusal leaves the membership as it was.
    if _joining(patch) - set(group.members) or _leaving(patch, group.members):
        check_role_assignment(request, user, group.roles, scope_of(group))

    added: set[str] = set()
    removed: set[str] = set()
    for op in patch.operations or []:
        kind = _op_kind(op)
        # op.path may be a typed ``Path`` object; coerce to a plain string.
        path = str(op.path or "")
        value = op.value
        if kind in ("add", "replace") and path.lower().startswith("members"):
            for username in _member_values(value):
                store.add_member(group_id, username)
                added.add(username)
        elif kind == "remove" and path.lower().startswith("members"):
            m = _MEMBER_FILTER_RE.search(path)
            targets = [m.group(1)] if m else _member_values(value)
            if not targets:  # remove with no filter -> clear membership
                targets = list(store.get(group_id).members)
            # The whole removal set is checked before any of it is applied, so a
            # refusal cannot leave earlier members already detached.
            store.protected.check_member_removal(group_id, targets)
            for username in targets:
                store.remove_member(group_id, username)
                removed.add(username)

    sync_account_groups_for_membership_change(account_store, group_id, added=added, removed=removed)
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
    if members:
        # Every member loses the roles the group carries.
        check_role_assignment(request, user, group.roles, scope_of(group))
    # Checked before the first detach, so deleting the admin group with a recovery
    # credential in it refuses whole rather than part-way through.
    store.protected.check_member_removal(group_id, members)
    for username in members:
        store.remove_member(group_id, username)
    sync_account_groups_for_membership_change(account_store, group_id, removed=members)
    store.delete(group_id)
    request_ch_rbac_reconcile(request.app.state)
    logger.info("SCIM group deleted", group=group_id)
    return Response(status_code=204)


def _op_kind(op) -> str:
    """The lowercase verb of a PatchOp operation (add/remove/replace)."""
    return getattr(op.op, "value", str(op.op)).lower()


def _joining(patch) -> set[str]:
    """Every username a group PatchOp adds or replaces into ``members``."""
    joining: set[str] = set()
    for op in patch.operations or []:
        if _op_kind(op) in ("add", "replace") and str(op.path or "").lower().startswith("members"):
            joining.update(_member_values(op.value))
    return joining


def _leaving(patch, members: list[str]) -> set[str]:
    """Every current member a group PatchOp removes; a remove naming no one clears the group."""
    leaving: set[str] = set()
    for op in patch.operations or []:
        path = str(op.path or "")
        if _op_kind(op) != "remove" or not path.lower().startswith("members"):
            continue
        match = _MEMBER_FILTER_RE.search(path)
        targets = [match.group(1)] if match else _member_values(op.value)
        leaving.update(targets or members)
    return leaving & set(members)


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
