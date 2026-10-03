#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/sigma.py
#  Purpose:      REST API for Sigma rule conversion and source mapping
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Sigma router - rule listing, field mappings, and view generation.

Wraps ``SigmaSourceMapper`` for REST access to Sigma field resolution,
``SigmaCatalogStore`` for rule listing, and ``SigmaPropagator`` for turning
selected rules into DFE rules.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from dfe_engine.api.deps import CurrentUser, HuntConfigReg, RuleReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams
from dfe_engine.api.review import set_review_headers
from dfe_engine.api.task_manager import TaskInfo, TaskManager, TaskStatus
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.clickhouse.quoting import plain_table_name
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.sigma.catalog import (
    SigmaCatalogStore,
    SigmaProviderStore,
    SigmaSelectionStore,
    sync_provider,
)
from dfe_engine.sigma.propagation import SigmaPropagator
from dfe_engine.sigma.providers import ProviderConfig, ProviderKind, build_provider
from dfe_engine.sigma.views import (
    SigmaViewColumn,
    SigmaViewDefinition,
    SigmaViewError,
    SigmaViewStore,
)
from dfe_engine.source.registry import SourceNotFoundError

router = APIRouter(prefix="/sigma", tags=["sigma"])

_READ = Depends(require_action(scopes_dict["sigma_read"]))
_WRITE = Depends(require_action(scopes_dict["sigma_write"]))
# Provider register/update/delete accept a git URL / local directory (SSRF +
# path-reach) - admin-only, above the sigma:write a data_analyst holds (S1).
_ADMIN = Depends(require_action(scopes_dict["sigma_admin"]))


# -- Response models -----------------------------------------


class FieldMapping(BaseModel):
    """A single Sigma field -> column mapping."""

    sigma_field: str
    column_name: str


class SigmaViewResult(BaseModel):
    """Generated Sigma view DDL for a source."""

    source_name: str
    ddl: str | None = Field(description="DDL for the view, or null if no mappings")


class SourceMappingSummary(BaseModel):
    """Sigma mapping summary for a source."""

    source_name: str
    field_count: int
    mappings: list[FieldMapping]


class LogsourceMatch(BaseModel):
    """A source matching a Sigma logsource selector."""

    source: str
    display_name: str = ""


class SigmaViewWriteRequest(BaseModel):
    """Create/replace body for a source's Sigma view definition (source_name is the path).

    Each column maps a Sigma field to a real source column OR a path inside the
    source's ``_json`` payload (a JSON-derived column); the ``SigmaViewColumn``
    validator enforces exactly one per column.
    """

    description: str = Field(default="", description="Human-readable description")
    columns: list[SigmaViewColumn] = Field(
        default_factory=list, description="Sigma-aligned column mappings"
    )
    include_source_columns: bool = Field(
        default=True, description="Also SELECT * (keep the base table columns in the view)"
    )


class SigmaViewSummary(BaseModel):
    """A row in the stored-view-definition list."""

    source_name: str
    description: str = ""
    column_count: int = 0
    json_derived_count: int = 0


# -- Dependencies --------------------------------------------


def _view_store_or_none(request: Request) -> SigmaViewStore | None:
    """A SigmaViewStore when gitops is enabled, else None (generation degrades)."""
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        return None
    return SigmaViewStore(gc)


def _get_source_mapper(request: Request):
    """Build a SigmaSourceMapper from app state registries.

    A stored view definition (when gitops is enabled) drives generation over the
    static field maps, so the mapper is handed the SigmaViewStore too.
    """
    from dfe_engine.api.deps import _registries
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    source_registry = _registries.get("source")
    if source_registry is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "SourceRegistry not configured - required for Sigma mappings",
            },
        )

    fieldmap_registry = _registries.get("fieldmap")
    return SigmaSourceMapper(
        source_registry=source_registry,
        registry=None,
        field_map_registry=fieldmap_registry,
        view_store=_view_store_or_none(request),
    )


def _source_not_found(source_name: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"Source '{source_name}' not found"},
    )


# -- Endpoints -----------------------------------------------


@router.get(
    "/mappings/{source_name}",
    response_model=SourceMappingSummary,
    dependencies=[Depends(require_action(scopes_dict["sigma_read"]))],
)
async def get_field_mappings(
    source_name: str,
    request: Request,
    user: CurrentUser,
    _auth: None = Depends(require_action(scopes_dict["sigma_read"])),
) -> SourceMappingSummary:
    """Get Sigma field mappings for a source."""
    mapper = _get_source_mapper(request)
    try:
        mappings = mapper.get_field_mappings(source_name)
    except KeyError, FileNotFoundError, SourceNotFoundError:
        raise _source_not_found(source_name)
    items = [FieldMapping(sigma_field=k, column_name=v) for k, v in mappings.items()]
    return SourceMappingSummary(
        source_name=source_name,
        field_count=len(items),
        mappings=items,
    )


# -- View definition CRUD (Task A/B) -------------------------
#
# A stored, operator-editable Sigma view definition per source (gitcrud
# sigma_views, keyed by source name). Unlike the static field maps (real column
# -> Sigma field), a definition can also declare columns DERIVED FROM a path
# inside the source's _json payload - the meta schema only knows the fixed
# _timestamp_load / _org_id / _source / _raw / _json columns. The definition is
# the SSoT the generate action (POST below) renders DDL from. All governed by
# sigma:read (reads) / sigma:write (mutations); gitops must be enabled (else 503).


@router.get("/views", response_model=PaginatedResponse[SigmaViewSummary], dependencies=[_READ])
async def list_sigma_view_definitions(
    request: Request,
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
) -> PaginatedResponse[SigmaViewSummary]:
    """List the sources that have a stored Sigma view definition, paginated."""
    store = SigmaViewStore(_gitcrud(request))
    items = [SigmaViewSummary(**row) for row in store.summaries()]
    return PaginatedResponse.from_list(items, pagination.page, pagination.per_page)


@router.get("/views/{source_name}", response_model=SigmaViewDefinition, dependencies=[_READ])
async def get_sigma_view_definition(
    source_name: str, request: Request, user: CurrentUser
) -> SigmaViewDefinition:
    """Get a source's stored Sigma view definition."""
    store = SigmaViewStore(_gitcrud(request))
    try:
        return store.get(source_name)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"code": "not_found", "message": f"no sigma view for '{source_name}'"},
        ) from exc


@router.put("/views/{source_name}", response_model=SigmaViewDefinition, dependencies=[_WRITE])
async def put_sigma_view_definition(
    source_name: str,
    body: SigmaViewWriteRequest,
    request: Request,
    user: CurrentUser,
) -> SigmaViewDefinition:
    """Create or replace a source's Sigma view definition."""
    definition = SigmaViewDefinition(
        source_name=source_name,
        description=body.description,
        columns=body.columns,
        include_source_columns=body.include_source_columns,
    )
    store = SigmaViewStore(_gitcrud(request))
    saved = await asyncio.to_thread(store.save, definition, user.user_id)
    audit_resource_change(user.user_id, "sigma_view", source_name, "updated")
    return saved


@router.delete("/views/{source_name}", status_code=204, dependencies=[_WRITE])
async def delete_sigma_view_definition(
    source_name: str, request: Request, user: CurrentUser
) -> None:
    """Delete a source's Sigma view definition."""
    store = SigmaViewStore(_gitcrud(request))
    try:
        await asyncio.to_thread(store.delete, source_name, user.user_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"code": "not_found", "message": f"no sigma view for '{source_name}'"},
        ) from exc
    audit_resource_change(user.user_id, "sigma_view", source_name, "deleted")


# -- View DDL generation / preview ---------------------------


@router.post("/views/{source_name}", response_model=SigmaViewResult, dependencies=[_WRITE])
async def generate_sigma_view(
    source_name: str,
    request: Request,
    user: CurrentUser,
    database: str = Query("default", description="Target database"),
) -> SigmaViewResult:
    """Generate (preview) Sigma view DDL for a source.

    A stored view definition drives generation - including its JSON-derived
    columns - when one exists; otherwise falls back to the static field maps via
    the source mapper. Returns the DDL string; does NOT execute it against
    ClickHouse.
    """
    store = _view_store_or_none(request)
    if store is not None and store.exists(source_name):
        try:
            ddl = store.generate_ddl(source_name, db=database)
        except SigmaViewError as exc:
            raise HTTPException(422, detail={"code": "invalid_view", "message": str(exc)}) from exc
        return SigmaViewResult(source_name=source_name, ddl=ddl)

    mapper = _get_source_mapper(request)
    try:
        ddl = mapper.generate_sigma_view(source_name, database)
    except KeyError, FileNotFoundError, SourceNotFoundError:
        raise _source_not_found(source_name)
    except SigmaViewError as exc:
        raise HTTPException(422, detail={"code": "invalid_view", "message": str(exc)}) from exc
    return SigmaViewResult(source_name=source_name, ddl=ddl)


@router.post("/views", response_model=list[SigmaViewResult], dependencies=[_WRITE])
async def generate_all_sigma_views(
    request: Request,
    user: CurrentUser,
    database: str = Query("default", description="Target database"),
    enabled_only: bool = Query(True, description="Only generate for enabled sources"),
) -> list[SigmaViewResult]:
    """Generate Sigma view DDL for all sources (stored definitions win over field maps)."""
    mapper = _get_source_mapper(request)
    views = mapper.generate_all_sigma_views(database, enabled_only=enabled_only)
    return [SigmaViewResult(source_name=src, ddl=ddl) for src, ddl in views.items()]


@router.get(
    "/logsource",
    response_model=list[LogsourceMatch],
    dependencies=[Depends(require_action(scopes_dict["sigma_read"]))],
)
async def find_sources_for_logsource(
    request: Request,
    user: CurrentUser,
    product: str | None = Query(None, description="Sigma logsource product (e.g. windows, aws)"),
    category: str | None = Query(
        None, description="Sigma logsource category (e.g. process_creation)"
    ),
    service: str | None = Query(
        None, description="Sigma logsource service (e.g. sysmon, security)"
    ),
    _auth: None = Depends(require_action(scopes_dict["sigma_read"])),
) -> list[LogsourceMatch]:
    """Find sources matching a Sigma logsource selector."""
    mapper = _get_source_mapper(request)
    sources = mapper.get_sources_for_logsource(
        product=product or "",
        category=category or "",
        service=service or "",
    )
    return [
        LogsourceMatch(
            source=s.source,
            display_name=getattr(s, "display_name", "") or "",
        )
        for s in sources
    ]


# == Sigma rule catalogue: providers + id-keyed CRUD store + selection ==========
#
# A pluggable provider fetches external sigma rules (SigmaHQ git repo default,
# Valhalla, a local import dir) which UPSERT by id into ONE gitcrud-backed
# catalogue; a separate selection list is the meta-level "which rules to
# implement". All governed by sigma:read (reads) / sigma:write (mutations).


# -- Models --------------------------------------------------


class SyncReportModel(BaseModel):
    """Counts from a provider sync into the catalogue."""

    source: str
    total: int
    added: int
    updated: int
    skipped: int
    merged: int
    committed: bool
    commit_sha: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SyncResponse(BaseModel):
    """Submit/poll envelope for a provider sync (submit -> poll, like the sampler)."""

    task_id: str = Field(description="Task ID; poll via GET /sigma/syncs/{task_id}")
    status: TaskStatus
    report: SyncReportModel | None = Field(default=None, description="Present once completed")
    error: str | None = Field(default=None, description="Present on failure")


class CatalogRuleSummary(BaseModel):
    """A catalogue row (no detection payload) for the paginated list."""

    id: str
    title: str = ""
    origin: str = ""
    upstream_modified: str | None = None
    local_edited: bool = False
    drift: bool = False
    selected: bool = False


class CatalogRuleDetail(BaseModel):
    """A full stored rule: normalised content + provenance + selection state."""

    id: str
    title: str = ""
    rule: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    selected: bool = False


class RuleEditRequest(BaseModel):
    """Operator edit of a rule's content (marks it locally edited -> survives re-import)."""

    rule: dict[str, Any]
    title: str | None = None


class SelectionResponse(BaseModel):
    """The curated selection list (rule ids)."""

    rules: list[str] = Field(default_factory=list)


# -- Store / provider dependencies ---------------------------


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _catalog(request: Request) -> SigmaCatalogStore:
    return SigmaCatalogStore(_gitcrud(request))


def _selection_store(request: Request) -> SigmaSelectionStore:
    return SigmaSelectionStore(_gitcrud(request))


def _provider_store(request: Request) -> SigmaProviderStore:
    return SigmaProviderStore(_gitcrud(request))


def _task_manager(request: Request) -> TaskManager:
    return request.app.state.task_manager


def _sigma_work_dir(request: Request) -> Path:
    """Persistent git-clone cache root for git-repo providers."""
    settings = request.app.state.settings
    base = getattr(settings, "config_dir", "") or ""
    if base:
        return Path(base) / ".sigma-cache"
    import tempfile

    return Path(tempfile.gettempdir()) / "dfe-sigma-cache"


def _make_provider(request: Request, config: ProviderConfig):
    secrets = getattr(request.app.state, "dfe_secrets", None)
    return build_provider(config, secrets=secrets, work_dir=_sigma_work_dir(request))


def _confine_provider_url(url: str, field: str) -> None:
    """Confine a provider's outbound URL (SSRF defence, S1).

    http(s) only. A LITERAL-IP host is checked directly (blocks loopback,
    link-local incl. the 169.254.169.254 metadata endpoint, unspecified, multicast,
    reserved - even the trailing-dot form, no DNS needed). A HOSTNAME is resolved
    BEST-EFFORT and rejected if it resolves to such an address; if resolution is
    UNAVAILABLE (a network-restricted runner / CI) it is NOT hard-failed - the
    ``DFE_SIGMA_ALLOWED_HOSTS`` allowlist + pin-at-connect (follow-up) are the
    backstop. Private RFC1918 hosts are NOT blocked (internal feeds are legitimate).
    """
    import ipaddress
    import os
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            422, detail={"code": "bad_url_scheme", "message": f"{field} must be http(s)"}
        )
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise HTTPException(422, detail={"code": "bad_url", "message": f"{field} has no host"})
    if host in ("localhost", "metadata", "metadata.google.internal"):
        raise HTTPException(
            422, detail={"code": "host_blocked", "message": f"{field} host {host!r} is blocked"}
        )

    def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_multicast
            or ip.is_reserved
        )

    # A literal-IP host is checked DIRECTLY (no DNS) - blocks 169.254.169.254 /
    # 127.0.0.1 / 0.0.0.0 and their trailing-dot forms even with no network.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _blocked(literal):
        raise HTTPException(
            422,
            detail={
                "code": "host_blocked",
                "message": f"{field} host {host!r} is blocked ({literal})",
            },
        )

    # A hostname (or decimal/octal numeric) is resolved BEST-EFFORT: reject if it
    # resolves to a blocked address, but if resolution is UNAVAILABLE (a
    # network-restricted CI runner) do NOT hard-fail - the allowlist below +
    # pin-at-connect (follow-up) are the backstop.
    if literal is None:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError:
            infos = []
        for info in infos:
            if _blocked(ipaddress.ip_address(info[4][0])):
                raise HTTPException(
                    422,
                    detail={
                        "code": "host_blocked",
                        "message": f"{field} host {host!r} resolves to a blocked address",
                    },
                )
    allowed = [
        h.strip().lower()
        for h in os.environ.get("DFE_SIGMA_ALLOWED_HOSTS", "").split(",")
        if h.strip()
    ]
    if allowed and host not in allowed:
        raise HTTPException(
            422,
            detail={
                "code": "host_not_allowed",
                "message": f"{field} host {host!r} not in DFE_SIGMA_ALLOWED_HOSTS",
            },
        )


def _validate_provider_reach(request: Request, config: ProviderConfig) -> None:
    """Confine a provider's reach at register/update time (S1, defence in depth on
    top of the sigma:admin gate + the ProviderConfig file:// reject).

    - local_files ``directory`` MUST resolve UNDER the engine config dir, so a
      provider cannot read arbitrary server files.
    - a git_repo ``url`` and a valhalla ``base_url`` are confined by
      :func:`_confine_provider_url` (http(s), no loopback/link-local/metadata host,
      and the ``DFE_SIGMA_ALLOWED_HOSTS`` allowlist when set).
    """
    if config.kind == ProviderKind.LOCAL_FILES:
        directory = str(config.options.get("directory", "")).strip()
        if directory:
            base = Path(getattr(request.app.state.settings, "config_dir", "") or "config").resolve()
            raw = Path(directory)
            target = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
            if not target.is_relative_to(base):
                raise HTTPException(
                    422,
                    detail={
                        "code": "path_escape",
                        "message": f"local_files directory must resolve under {base}",
                    },
                )
    elif config.kind == ProviderKind.GIT_REPO:
        _confine_provider_url(str(config.options.get("url", "")), "git url")
    elif config.kind == ProviderKind.VALHALLA:
        base_url = str(config.options.get("base_url", "")).strip()
        if base_url:  # empty -> the provider's built-in Nextron API default (safe)
            _confine_provider_url(base_url, "valhalla base_url")


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            400, detail={"code": "bad_since", "message": "since must be ISO 8601"}
        ) from exc


def _sync_response(info: TaskInfo) -> SyncResponse:
    report = None
    if info.status == TaskStatus.COMPLETED and isinstance(info.result, dict):
        report = SyncReportModel.model_validate(info.result)
    return SyncResponse(task_id=info.id, status=info.status, report=report, error=info.error)


def _detail(doc: dict[str, Any], selected: bool) -> CatalogRuleDetail:
    return CatalogRuleDetail(
        id=str(doc.get("id", "")),
        title=doc.get("title", "") or "",
        rule=doc.get("rule", {}) or {},
        provenance=doc.get("provenance", {}) or {},
        selected=selected,
    )


async def _run_sync(provider, catalog, actor, since, *, task) -> dict[str, Any]:
    """TaskManager coroutine: fetch from the provider and upsert into the catalogue."""
    report = await sync_provider(provider, catalog, actor, since=since)
    return report.as_dict()


# -- Provider CRUD -------------------------------------------


@router.get("/providers", response_model=list[ProviderConfig], dependencies=[_READ])
async def list_providers(request: Request, user: CurrentUser) -> list[ProviderConfig]:
    """List provider configs (the built-in SigmaHQ default is present OOTB)."""
    return _provider_store(request).list_configs()


@router.post("/providers", response_model=ProviderConfig, status_code=201, dependencies=[_ADMIN])
async def register_provider(
    body: ProviderConfig, request: Request, user: CurrentUser
) -> ProviderConfig:
    """Register (or overwrite) a provider config."""
    _validate_provider_reach(request, body)
    saved = await asyncio.to_thread(_provider_store(request).save_config, body, user.user_id)
    audit_resource_change(user.user_id, "sigma_provider", body.name, "registered")
    return saved


@router.get("/providers/{name}", response_model=ProviderConfig, dependencies=[_READ])
async def get_provider(name: str, request: Request, user: CurrentUser) -> ProviderConfig:
    """Get one provider config."""
    try:
        return _provider_store(request).get_config(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"provider '{name}' not found"}
        ) from exc


@router.put("/providers/{name}", response_model=ProviderConfig, dependencies=[_ADMIN])
async def update_provider(
    name: str, body: ProviderConfig, request: Request, user: CurrentUser
) -> ProviderConfig:
    """Update a provider config. The path name wins over the body name."""
    config = body.model_copy(update={"name": name})
    _validate_provider_reach(request, config)
    saved = await asyncio.to_thread(_provider_store(request).save_config, config, user.user_id)
    audit_resource_change(user.user_id, "sigma_provider", name, "updated")
    return saved


@router.post("/providers/{name}/enable", response_model=ProviderConfig, dependencies=[_WRITE])
async def enable_provider(name: str, request: Request, user: CurrentUser) -> ProviderConfig:
    """Enable a provider (polling budget applies)."""
    try:
        saved = await asyncio.to_thread(
            _provider_store(request).set_enabled, name, True, user.user_id
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"provider '{name}' not found"}
        ) from exc
    audit_resource_change(user.user_id, "sigma_provider", name, "enabled")
    return saved


@router.post("/providers/{name}/disable", response_model=ProviderConfig, dependencies=[_WRITE])
async def disable_provider(name: str, request: Request, user: CurrentUser) -> ProviderConfig:
    """Disable a provider (blocks scheduled polling; a manual sync is refused)."""
    try:
        saved = await asyncio.to_thread(
            _provider_store(request).set_enabled, name, False, user.user_id
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"provider '{name}' not found"}
        ) from exc
    audit_resource_change(user.user_id, "sigma_provider", name, "disabled")
    return saved


@router.delete("/providers/{name}", status_code=204, dependencies=[_ADMIN])
async def delete_provider(name: str, request: Request, user: CurrentUser) -> None:
    """Delete a stored provider config (a built-in default reverts to its default)."""
    try:
        await asyncio.to_thread(_provider_store(request).delete_config, name, user.user_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"provider '{name}' not found"}
        ) from exc
    audit_resource_change(user.user_id, "sigma_provider", name, "deleted")


# -- Provider sync (submit -> poll) --------------------------


@router.post("/providers/{name}/sync", response_model=SyncResponse, dependencies=[_WRITE])
async def sync_provider_endpoint(
    name: str,
    request: Request,
    user: CurrentUser,
    since: str | None = Query(None, description="Only import rules modified since (ISO 8601)"),
    wait: float = Query(10.0, ge=0, le=120, description="Seconds to block for inline completion"),
) -> SyncResponse:
    """Trigger a provider sync into the catalogue.

    Submitted to the task manager (a git-repo clone is long-running); blocks up to
    ``wait`` seconds for inline completion, else returns ``pending`` - poll via
    GET /sigma/syncs/{task_id}.
    """
    store = _provider_store(request)
    try:
        config = store.get_config(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"provider '{name}' not found"}
        ) from exc
    if not config.enabled:
        raise HTTPException(
            409, detail={"code": "provider_disabled", "message": f"provider '{name}' is disabled"}
        )
    since_dt = _parse_since(since)
    provider = _make_provider(request, config)
    catalog = _catalog(request)
    manager = _task_manager(request)
    info = manager.submit("sigma:sync", _run_sync, provider, catalog, user.user_id, since_dt)
    audit_resource_change(user.user_id, "sigma_provider", name, "synced")
    if wait > 0:
        info = await manager.await_terminal(info.id, wait) or info
    return _sync_response(info)


@router.get("/syncs/{task_id}", response_model=SyncResponse, dependencies=[_READ])
async def get_sync(task_id: str, request: Request, user: CurrentUser) -> SyncResponse:
    """Poll a provider-sync task."""
    info = _task_manager(request).get(task_id)
    # Match the EXACT kind (mirroring GET /sigma/propagations). A startswith
    # 'sigma:' also let a 'sigma:propagate' task through, whose result dict then
    # failed SyncReportModel validation -> 500 instead of the 404 contract.
    if info is None or info.kind != "sigma:sync":
        raise HTTPException(404, detail={"code": "not_found", "message": "sync task not found"})
    return _sync_response(info)


# -- Rule catalogue (paginated list / get / edit / adopt / delete) ----


@router.get(
    "/catalogue", response_model=PaginatedResponse[CatalogRuleSummary], dependencies=[_READ]
)
async def list_catalogue(
    request: Request,
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
    q: str | None = Query(None, description="Search id/title (case-insensitive)"),
    selected: bool | None = Query(None, description="Filter by selection state"),
) -> PaginatedResponse[CatalogRuleSummary]:
    """Paginated catalogue of stored sigma rules, marked with selection state."""
    catalog = _catalog(request)
    selected_ids = set(_selection_store(request).list_selected())
    rows = catalog.summaries()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["id"].lower() or ql in (r["title"] or "").lower()]
    items: list[CatalogRuleSummary] = []
    for r in rows:
        is_sel = r["id"] in selected_ids
        if selected is not None and is_sel != selected:
            continue
        items.append(CatalogRuleSummary(**r, selected=is_sel))
    items.sort(key=lambda x: (x.title or x.id).lower())
    return PaginatedResponse.from_list(items, pagination.page, pagination.per_page)


@router.get("/catalogue/{rule_id}", response_model=CatalogRuleDetail, dependencies=[_READ])
async def get_catalogue_rule(
    rule_id: str, request: Request, user: CurrentUser
) -> CatalogRuleDetail:
    """Get one stored rule (full content + provenance)."""
    try:
        doc = _catalog(request).get_rule(rule_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"rule '{rule_id}' not found"}
        ) from exc
    return _detail(doc, _selection_store(request).is_selected(rule_id))


@router.put("/catalogue/{rule_id}", response_model=CatalogRuleDetail, dependencies=[_WRITE])
async def edit_catalogue_rule(
    rule_id: str, body: RuleEditRequest, request: Request, user: CurrentUser
) -> CatalogRuleDetail:
    """Edit a rule's content. Marks it locally edited so a re-import preserves it."""
    catalog = _catalog(request)
    try:
        doc = await asyncio.to_thread(
            catalog.edit_rule, rule_id, body.rule, user.user_id, title=body.title
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"rule '{rule_id}' not found"}
        ) from exc
    audit_resource_change(user.user_id, "sigma_rule", rule_id, "edited")
    return _detail(doc, _selection_store(request).is_selected(rule_id))


@router.post("/catalogue/{rule_id}/adopt", response_model=CatalogRuleDetail, dependencies=[_WRITE])
async def adopt_catalogue_rule(
    rule_id: str, request: Request, user: CurrentUser
) -> CatalogRuleDetail:
    """Detach a rule from upstream (pin its content; a re-import records drift only)."""
    catalog = _catalog(request)
    try:
        doc = await asyncio.to_thread(catalog.adopt_rule, rule_id, user.user_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"rule '{rule_id}' not found"}
        ) from exc
    audit_resource_change(user.user_id, "sigma_rule", rule_id, "adopted")
    return _detail(doc, _selection_store(request).is_selected(rule_id))


@router.delete("/catalogue/{rule_id}", status_code=204, dependencies=[_WRITE])
async def delete_catalogue_rule(rule_id: str, request: Request, user: CurrentUser) -> None:
    """Remove a rule from the catalogue."""
    try:
        await asyncio.to_thread(_catalog(request).delete_rule, rule_id, user.user_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"rule '{rule_id}' not found"}
        ) from exc
    audit_resource_change(user.user_id, "sigma_rule", rule_id, "deleted")


# -- Selection CRUD (the meta-level "which rules to implement") -------


@router.get("/selected", response_model=SelectionResponse, dependencies=[_READ])
async def list_selected(request: Request, user: CurrentUser) -> SelectionResponse:
    """List the ids of rules selected to implement."""
    return SelectionResponse(rules=_selection_store(request).list_selected())


@router.post("/catalogue/{rule_id}/select", response_model=SelectionResponse, dependencies=[_WRITE])
async def select_rule(rule_id: str, request: Request, user: CurrentUser) -> SelectionResponse:
    """Select a catalogued rule to implement."""
    if not _catalog(request).exists(rule_id):
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"rule '{rule_id}' not in catalogue"}
        )
    store = _selection_store(request)
    await asyncio.to_thread(store.select, rule_id, user.user_id)
    audit_resource_change(user.user_id, "sigma_selection", rule_id, "selected")
    return SelectionResponse(rules=store.list_selected())


@router.post(
    "/catalogue/{rule_id}/deselect", response_model=SelectionResponse, dependencies=[_WRITE]
)
async def deselect_rule(rule_id: str, request: Request, user: CurrentUser) -> SelectionResponse:
    """Deselect a rule (idempotent)."""
    store = _selection_store(request)
    await asyncio.to_thread(store.deselect, rule_id, user.user_id)
    audit_resource_change(user.user_id, "sigma_selection", rule_id, "deselected")
    return SelectionResponse(rules=store.list_selected())


# == Propagation: selected sigma rules -> sigma-bound DFE rules + hunts ==========
#
# The final pipeline stage. POST /sigma/propagate GENERATES a DFE detection rule
# per (selected sigma rule x matching source), converting the sigma detection to a
# ClickHouse WHERE over the source's `{source}_sigma` view (5b), then binds each
# generated rule into a per-source hunt. It is a generator over the EXISTING rule +
# hunt registries (never a second CRUD). Drift is honoured: a binding derived from
# a locally-edited/drifted sigma rule - or one hand-edited since generation - is
# reported skipped, not clobbered, unless force=true. Governed by sigma:read
# (reads) / sigma:write (propagate + delete); gitops must be enabled (else 503).


# -- Models --------------------------------------------------


class PropagateRequest(BaseModel):
    """Options for a propagate run over the current sigma selection."""

    force: bool = Field(
        default=False,
        description="Regenerate even over drifted / hand-edited bindings (else skipped)",
    )
    create_hunts: bool = Field(
        default=True, description="Also bind each generated rule into a per-source hunt"
    )
    hunt_cron: str = Field(
        default="*/15 * * * *", description="Cron for a newly-created per-source hunt"
    )
    hunt_target_table: str = Field(
        default="detection", description="global_target_table_name for a new hunt"
    )
    hunt_customers: list[str] = Field(
        default_factory=lambda: ["default"],
        description="customers for a new hunt (operator adjusts later via PUT /hunts)",
    )

    @field_validator("hunt_target_table")
    @classmethod
    def _target_is_a_table_name(cls, value: str) -> str:
        # The hunt runner splices this into INSERT INTO, so it may only name a table.
        plain_table_name(value)
        return value


class PropagationReportModel(BaseModel):
    """Counts + ids from a propagate run."""

    total_selected: int = 0
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped_drifted: list[dict[str, Any]] = Field(default_factory=list)
    skipped_no_source: list[str] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    hunts_touched: list[str] = Field(default_factory=list)
    stale_bindings: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Bindings left behind by a deselect (still firing until deleted)",
    )
    warnings: list[str] = Field(default_factory=list)
    review_required: bool = Field(
        default=False,
        description="A generated rule or hunt was routed to a review branch, not to the "
        "branch the hunt runner syncs; nothing here runs until those branches merge",
    )
    review_branches: list[str] = Field(
        default_factory=list, description="Review branches the run's writes landed on"
    )
    pr_urls: list[str] = Field(
        default_factory=list, description="Review PRs opened for the run's writes"
    )


class PropagateResponse(BaseModel):
    """Submit/poll envelope for a propagate run (submit -> poll, like the sampler)."""

    task_id: str = Field(description="Task ID; poll via GET /sigma/propagations/{task_id}")
    status: TaskStatus
    report: PropagationReportModel | None = Field(
        default=None, description="Present once completed"
    )
    error: str | None = Field(default=None, description="Present on failure")


class BindingSummary(BaseModel):
    """A generated sigma-bound rule with its live drift state."""

    rule_id: str
    sigma_rule_id: str
    display_name: str = ""
    source: str = ""
    source_table: str = ""
    severity: str = "medium"
    hunts: list[str] = Field(default_factory=list)
    hand_edited: bool = False
    orphaned: bool = False
    selected: bool = True
    stale: bool = False
    drift: bool = False


# -- Dependencies --------------------------------------------


def _propagator(
    request: Request,
    rules: RuleReg,
    hunts: HuntConfigReg,
    actor: str,
) -> SigmaPropagator:
    """Assemble the SigmaPropagator from the sigma stores + rule/hunt registries.

    The source mapper is handed the SigmaViewStore too, so logsource->source
    binding sees the same sources the view generation does. Raises 503 (via the
    _gitcrud / _get_source_mapper / RuleReg / HuntConfigReg deps) when a required
    store is unconfigured.
    """
    return SigmaPropagator(
        catalog=_catalog(request),
        selection=_selection_store(request),
        source_mapper=_get_source_mapper(request),
        rule_registry=rules,
        hunt_registry=hunts,
        actor=actor,
        database=request.app.state.settings.clickhouse.effective_data_database,
    )


async def _run_propagate(propagator: SigmaPropagator, options: PropagateRequest, *, task) -> dict:
    """TaskManager coroutine: run propagation off the event loop (blocking git I/O)."""
    report = await asyncio.to_thread(
        propagator.propagate,
        force=options.force,
        create_hunts=options.create_hunts,
        hunt_cron=options.hunt_cron,
        hunt_target_table=options.hunt_target_table,
        hunt_customers=options.hunt_customers,
    )
    return report.as_dict()


def _propagate_response(info: TaskInfo) -> PropagateResponse:
    report = None
    if info.status == TaskStatus.COMPLETED and isinstance(info.result, dict):
        report = PropagationReportModel.model_validate(info.result)
    return PropagateResponse(task_id=info.id, status=info.status, report=report, error=info.error)


def _apply_report_headers(response: Response, report: PropagationReportModel | None) -> None:
    """Carry a completed run's review state in the headers the hunt/rule writes use.

    A run that fanned out over several bindings opened several PRs, so the header
    names the first and the report body carries the rest.
    """
    if report is None:
        return
    set_review_headers(
        response,
        review_required=report.review_required,
        pr_url=report.pr_urls[0] if report.pr_urls else None,
    )


# -- Endpoints -----------------------------------------------


@router.post("/propagate", response_model=PropagateResponse, dependencies=[_WRITE])
async def propagate(
    request: Request,
    user: CurrentUser,
    rules: RuleReg,
    hunts: HuntConfigReg,
    response: Response,
    body: PropagateRequest | None = None,
    wait: float = Query(30.0, ge=0, le=120, description="Seconds to block for inline completion"),
) -> PropagateResponse:
    """Generate sigma-bound DFE rules + hunts from the current selection.

    For each SELECTED sigma rule, converts its detection to a ClickHouse WHERE over
    the matching source's ``{source}_sigma`` view and stores a DFE rule (with a
    ``sigma_rule_id`` back-reference), then binds it into a per-source hunt. Drifted
    or hand-edited bindings are reported ``skipped_drifted`` unless ``force``.
    Submitted to the task manager; blocks up to ``wait`` seconds for inline
    completion, else returns ``pending`` - poll via GET /sigma/propagations/{id}.

    A production+team run commits each generated rule and hunt to its own review
    branch rather than the branch the hunt runner syncs; the report carries every
    branch and PR, and ``X-DFE-Review-Required`` says so on a run that completed
    inline.
    """
    options = body or PropagateRequest()
    propagator = _propagator(request, rules, hunts, user.user_id)
    manager = _task_manager(request)
    info = manager.submit("sigma:propagate", _run_propagate, propagator, options)
    audit_resource_change(user.user_id, "sigma_propagation", "selection", "propagated")
    if wait > 0:
        info = await manager.await_terminal(info.id, wait) or info
    result = _propagate_response(info)
    _apply_report_headers(response, result.report)
    return result


@router.get("/propagations/{task_id}", response_model=PropagateResponse, dependencies=[_READ])
async def get_propagation(
    task_id: str, request: Request, user: CurrentUser, response: Response
) -> PropagateResponse:
    """Poll a propagate task."""
    info = _task_manager(request).get(task_id)
    if info is None or info.kind != "sigma:propagate":
        raise HTTPException(
            404, detail={"code": "not_found", "message": "propagation task not found"}
        )
    result = _propagate_response(info)
    _apply_report_headers(response, result.report)
    return result


@router.get("/bindings", response_model=PaginatedResponse[BindingSummary], dependencies=[_READ])
async def list_bindings(
    request: Request,
    user: CurrentUser,
    rules: RuleReg,
    hunts: HuntConfigReg,
    pagination: PaginationParams = Depends(),
    source: str | None = Query(None, description="Filter by source label"),
    drift: bool | None = Query(None, description="Filter by drift state"),
) -> PaginatedResponse[BindingSummary]:
    """Paginated list of the generated sigma-bound rules, with live drift state."""
    propagator = _propagator(request, rules, hunts, user.user_id)
    rows = await asyncio.to_thread(propagator.list_bindings)
    items = [BindingSummary(**row) for row in rows]
    if source is not None:
        items = [b for b in items if b.source == source]
    if drift is not None:
        items = [b for b in items if b.drift == drift]
    items.sort(key=lambda b: b.rule_id)
    return PaginatedResponse.from_list(items, pagination.page, pagination.per_page)


@router.get("/bindings/{rule_id}", response_model=BindingSummary, dependencies=[_READ])
async def get_binding(
    rule_id: str, request: Request, user: CurrentUser, rules: RuleReg, hunts: HuntConfigReg
) -> BindingSummary:
    """Get one generated sigma-bound rule (with live drift state)."""
    propagator = _propagator(request, rules, hunts, user.user_id)
    row = await asyncio.to_thread(propagator.get_binding, rule_id)
    if row is None:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"no sigma binding '{rule_id}'"}
        )
    return BindingSummary(**row)


@router.delete("/bindings/{rule_id}", status_code=204, dependencies=[_WRITE])
async def delete_binding(
    rule_id: str,
    request: Request,
    user: CurrentUser,
    rules: RuleReg,
    hunts: HuntConfigReg,
    response: Response,
) -> None:
    """Delete a generated binding (removes the rule and unlinks it from its hunt).

    A production+team delete is routed to a review branch, so the runner keeps
    compiling the binding until that branch is merged; ``X-DFE-Review-Required``
    says so.
    """
    propagator = _propagator(request, rules, hunts, user.user_id)
    routing = await asyncio.to_thread(propagator.delete_binding, rule_id)
    if routing is None:
        raise HTTPException(
            404, detail={"code": "not_found", "message": f"no sigma binding '{rule_id}'"}
        )
    set_review_headers(
        response, review_required=routing.review_required, pr_url=routing.first_pr_url
    )
    audit_resource_change(user.user_id, "sigma_binding", rule_id, "deleted")
