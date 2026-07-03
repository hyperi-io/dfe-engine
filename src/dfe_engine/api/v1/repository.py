#  Project:      dfe-engine
#  File:         repository.py
#  Purpose:      Repository router - scope-aligned small-object store for dfe-ui
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Repository router - UI preferences and small objects.

GET    /api/v1/repository/preferences                     -> effective merged prefs
PATCH  /api/v1/repository/preferences                     -> JSON merge-patch (user layer)
GET    /api/v1/repository/objects/{scope}/{scope_id}/{namespace}        -> list metadata
GET    /api/v1/repository/objects/{scope}/{scope_id}/{namespace}/{key}  -> raw bytes
PUT    /api/v1/repository/objects/{scope}/{scope_id}/{namespace}/{key}  -> store bytes
DELETE /api/v1/repository/objects/{scope}/{scope_id}/{namespace}/{key}  -> tombstone

Scopes: system / org / group / user. For scope=system the path scope_id
must be "-" (stored as ""). Reads that are not visible return 404;
forbidden writes return 403.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    Settings,
    check_action,
    is_action_allowed,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.models import AuthContext, Scope
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.repository.store import ConflictError, RepositoryStore, json_merge_patch

router = APIRouter(prefix="/repository", tags=["Repository"])

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_VALID_SCOPES = ("system", "org", "group", "user")

_PREFS_NAMESPACE = "preferences"
_PREFS_KEY = "default"


# ── Response models ──────────────────────────────────────────


class PreferencesResponse(BaseModel):
    """Effective merged preferences plus the caller's user-layer etag."""

    preferences: dict[str, Any]
    etag: str | None = None


class ObjectMetadata(BaseModel):
    """Metadata for a stored object (PUT response)."""

    scope: str
    scope_id: str
    namespace: str
    key: str
    content_type: str
    size: int
    updated_by: str
    updated_at: datetime
    etag: str


class ObjectEntry(BaseModel):
    """Metadata for one key in a namespace listing."""

    key: str
    content_type: str
    size: int
    updated_by: str
    updated_at: datetime
    etag: str


# ── Helpers ──────────────────────────────────────────────────


def _store(client: Any, settings: Any) -> RepositoryStore:
    return RepositoryStore(client, database=settings.repository.database)


def _validation_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "validation_error", "message": message},
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": "Object not found"},
    )


def _validate_names(namespace: str, key: str | None = None) -> None:
    if not _NAME_RE.match(namespace):
        raise _validation_error(f"Invalid namespace '{namespace}'")
    if key is not None and not _NAME_RE.match(key):
        raise _validation_error(f"Invalid key '{key}'")


def _map_scope(request: Request, scope: str, scope_id: str) -> tuple[Scope, str]:
    """Map path (scope, scope_id) to a Scope and the stored scope_id.

    system requires scope_id "-" (stored as ""); group looks up the owning
    org from the GroupStore (404 for unknown groups).
    """
    if scope not in _VALID_SCOPES:
        raise _validation_error(f"Invalid scope '{scope}' (system, org, group, user)")
    if scope == "system":
        if scope_id != "-":
            raise _validation_error("scope_id must be '-' for system scope")
        return Scope(), ""
    if scope == "org":
        return Scope(type="org", id=scope_id), scope_id
    if scope == "group":
        group = request.app.state.group_store.get(scope_id)
        if group is None:
            raise _not_found()
        return Scope(type="group", id=scope_id, org=group.scope_org), scope_id
    return Scope(type="user", id=scope_id), scope_id


def _can_read(request: Request, user: AuthContext, scope: str, scope_id: str) -> bool:
    """Membership-or-grant read rule (see module docstring)."""
    if scope == "system":
        return True
    if scope == "user" and user.user_id == scope_id:
        return True
    if scope == "org" and scope_id in user.org_ids:
        return True
    if scope == "group" and scope_id in user.groups:
        return True
    mapped, _ = _map_scope(request, scope, scope_id)
    return is_action_allowed(request, user, scopes_dict["repository_read"], scope=mapped)


def _require_read(request: Request, user: AuthContext, scope: str, scope_id: str) -> str:
    """404 (not 403) when the layer is not visible - existence stays hidden."""
    _, stored_id = _map_scope(request, scope, scope_id)
    if not _can_read(request, user, scope, scope_id):
        raise _not_found()
    return stored_id


def _require_write(request: Request, user: AuthContext, scope: str, scope_id: str) -> str:
    """Owner may write the own user layer; everything else needs repository:write."""
    mapped, stored_id = _map_scope(request, scope, scope_id)
    if scope == "user" and user.user_id == scope_id:
        return stored_id
    check_action(request, user, scopes_dict["repository_write"], scope=mapped)
    return stored_id


def _decode_json(record: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a stored value as a JSON object, tolerating junk layers."""
    try:
        doc = json.loads(record["value"])
    except (ValueError, UnicodeDecodeError):
        logger.warning(
            "Repository: unparseable preferences layer skipped",
            scope=record["scope"],
            scope_id=record["scope_id"],
        )
        return None
    return doc if isinstance(doc, dict) else None


def _preference_layers(user: AuthContext) -> list[tuple[str, str]]:
    """Merge order: system -> orgs (sorted) -> groups (sorted) -> user (wins)."""
    layers: list[tuple[str, str]] = [("system", "")]
    layers += [("org", org) for org in sorted(user.org_ids)]
    layers += [("group", group) for group in sorted(user.groups)]
    layers.append(("user", user.user_id))
    return layers


def _effective_preferences(
    store: RepositoryStore,
    user: AuthContext,
    *,
    user_doc_override: dict[str, Any] | None = None,
    user_etag_override: str | None = None,
) -> PreferencesResponse:
    """Deep-merge the caller's preference layers into one effective doc.

    ``user_doc_override`` lets PATCH splice in the doc it just wrote instead
    of racing the ReplacingMergeTree read-back.
    """
    merged: dict[str, Any] = {}
    user_etag: str | None = user_etag_override
    for scope, scope_id in _preference_layers(user):
        if scope == "user" and user_doc_override is not None:
            merged = json_merge_patch(merged, user_doc_override)
            continue
        record = store.get(scope, scope_id, _PREFS_NAMESPACE, _PREFS_KEY)
        if record is None:
            continue
        doc = _decode_json(record)
        if doc is None:
            continue
        merged = json_merge_patch(merged, doc)
        if scope == "user":
            user_etag = record["etag"]
    return PreferencesResponse(preferences=merged, etag=user_etag)


def _precondition_failed(current_etag: str | None) -> JSONResponse:
    headers = {"ETag": current_etag} if current_etag else None
    return JSONResponse(
        status_code=412,
        content={
            "code": "precondition_failed",
            "message": "If-Match does not match the stored etag",
        },
        headers=headers,
    )


# ── Preferences (dfe-ui fast path) ───────────────────────────


@router.get("/preferences", response_model=PreferencesResponse)
async def get_preferences(
    user: CurrentUser,
    ch_client: ClickHouseClient,
    settings: Settings,
):
    """Effective merged preferences for the caller (system -> org -> group -> user)."""
    store = _store(ch_client, settings)
    return _effective_preferences(store, user)


@router.patch("/preferences", response_model=PreferencesResponse)
async def patch_preferences(
    user: CurrentUser,
    ch_client: ClickHouseClient,
    settings: Settings,
    patch: dict[str, Any] = Body(..., description="RFC 7396 JSON merge patch"),
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Apply a JSON merge patch to the caller's USER preference layer.

    The theme toggle is one call: ``PATCH {"theme": "dark"}``. ``null``
    removes a user-layer key (falling back to the inherited value).
    """
    store = _store(ch_client, settings)
    current = store.get("user", user.user_id, _PREFS_NAMESPACE, _PREFS_KEY)
    current_doc = (_decode_json(current) or {}) if current else {}
    new_doc = json_merge_patch(current_doc, patch)

    payload = json.dumps(new_doc, separators=(",", ":")).encode("utf-8")
    if len(payload) > settings.repository.max_prefs_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "payload_too_large",
                "message": f"Preferences exceed {settings.repository.max_prefs_bytes} bytes",
            },
        )
    try:
        meta = store.put(
            "user",
            user.user_id,
            _PREFS_NAMESPACE,
            _PREFS_KEY,
            payload,
            "application/json",
            user.user_id,
            if_match=if_match,
        )
    except ConflictError as exc:
        return _precondition_failed(exc.current_etag)
    return _effective_preferences(
        store, user, user_doc_override=new_doc, user_etag_override=meta["etag"]
    )


# ── Objects (generic) ────────────────────────────────────────


@router.get("/objects/{scope}/{scope_id}/{namespace}", response_model=list[ObjectEntry])
async def list_objects(
    scope: str,
    scope_id: str,
    namespace: str,
    request: Request,
    user: CurrentUser,
    ch_client: ClickHouseClient,
    settings: Settings,
):
    """List object metadata in a (scope, scope_id, namespace)."""
    _validate_names(namespace)
    stored_id = _require_read(request, user, scope, scope_id)
    store = _store(ch_client, settings)
    return store.list(scope, stored_id, namespace)


@router.get("/objects/{scope}/{scope_id}/{namespace}/{key}")
async def get_object(
    scope: str,
    scope_id: str,
    namespace: str,
    key: str,
    request: Request,
    user: CurrentUser,
    ch_client: ClickHouseClient,
    settings: Settings,
):
    """Raw object bytes with the stored Content-Type and ETag headers."""
    _validate_names(namespace, key)
    stored_id = _require_read(request, user, scope, scope_id)
    store = _store(ch_client, settings)
    record = store.get(scope, stored_id, namespace, key)
    if record is None:
        raise _not_found()
    return Response(
        content=record["value"],
        media_type=record["content_type"] or "application/octet-stream",
        headers={"ETag": record["etag"]},
    )


@router.put("/objects/{scope}/{scope_id}/{namespace}/{key}", response_model=ObjectMetadata)
async def put_object(
    scope: str,
    scope_id: str,
    namespace: str,
    key: str,
    request: Request,
    user: CurrentUser,
    ch_client: ClickHouseClient,
    settings: Settings,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
):
    """Store raw bytes (body) under a scope-aligned key."""
    _validate_names(namespace, key)
    stored_id = _require_write(request, user, scope, scope_id)

    body = await request.body()
    if len(body) > settings.repository.max_object_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "payload_too_large",
                "message": f"Object exceeds {settings.repository.max_object_bytes} bytes",
            },
        )
    content_type = request.headers.get("Content-Type") or "application/octet-stream"

    store = _store(ch_client, settings)
    try:
        meta = store.put(
            scope,
            stored_id,
            namespace,
            key,
            body,
            content_type,
            user.user_id,
            if_match=if_match,
        )
    except ConflictError as exc:
        return _precondition_failed(exc.current_etag)
    audit_resource_change(
        user.user_id, "repository_object", f"{scope}/{scope_id}/{namespace}/{key}", "updated"
    )
    response.headers["ETag"] = meta["etag"]
    return ObjectMetadata(**meta)


@router.delete("/objects/{scope}/{scope_id}/{namespace}/{key}", status_code=204)
async def delete_object(
    scope: str,
    scope_id: str,
    namespace: str,
    key: str,
    request: Request,
    user: CurrentUser,
    ch_client: ClickHouseClient,
    settings: Settings,
):
    """Tombstone an object (INSERT with is_deleted=1, never a mutation)."""
    _validate_names(namespace, key)
    stored_id = _require_write(request, user, scope, scope_id)
    store = _store(ch_client, settings)
    if not store.delete(scope, stored_id, namespace, key):
        raise _not_found()
    audit_resource_change(
        user.user_id, "repository_object", f"{scope}/{scope_id}/{namespace}/{key}", "deleted"
    )
