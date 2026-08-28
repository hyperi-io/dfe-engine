#  Project:      dfe-engine
#  File:         api/v1/apps.py
#  Purpose:      One managed surface per deployed app instance
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Per-instance management for every deployed DFE app.

GET    /api/v1/apps                                   catalogue + deployed instances
POST   /api/v1/apps/{service}/instances               deploy an instance
GET    /api/v1/apps/{service}/{instance}              one instance, summarised
DELETE /api/v1/apps/{service}/{instance}              undeploy an instance
GET    /api/v1/apps/{service}/{instance}/values       the instance's overlay values
GET    /api/v1/apps/{service}/{instance}/scaling      the scaling dials
PUT    /api/v1/apps/{service}/{instance}/scaling      set the scaling dials
GET    /api/v1/apps/{service}/{instance}/files/{set}  list the files it consumes
GET    ...        /files/{set}/{filename}             read one
PUT    ...        /files/{set}/{filename}             write one
DELETE ...        /files/{set}/{filename}             remove one
GET    /api/v1/apps/{service}/{instance}/status       is it reporting, since when
GET    /api/v1/apps/{service}/{instance}/metrics      throughput, cpu, memory

Every mutation is a git commit into the deploy repo through the same gitcrud path
``api/v1/helm.py`` uses, so the protected-var policy, review routing and audit apply
unchanged. RBAC binds to the ``helmvars`` class because that is the resource being
written; a separate class would have to be granted everywhere for no gain.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import ClickHouseClient, CurrentUser, require_action
from dfe_engine.appmgmt import (
    AppInstance,
    DeployTarget,
    FileNotInSetError,
    InstanceExistsError,
    InvalidDialError,
    InvalidFilenameError,
    InvalidInstanceError,
    MetricsUnavailableError,
    OperationalReader,
    UnknownAppError,
    catalogue,
    files,
    instances,
    scaling,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud
from dfe_engine.gitcrud.commit_policy import (
    SUBJECT_MAX,
    CommitContext,
    CommitPolicyError,
    build_message,
    validate_change,
)
from dfe_engine.gitcrud.engine import ResourceNotFoundError, set_path
from dfe_engine.gitcrud.routing import ReviewRequiredError, route_write
from dfe_engine.governance import PolicyStore, ProtectedVarError

router = APIRouter(prefix="/apps", tags=["App Management"])

_CLASS = instances.HELMVARS_CLASS
_READ = Depends(require_action(scopes_dict["helmvars_read"]))
_WRITE = Depends(require_action(scopes_dict["helmvars_write"]))


# ── request and response models ───────────────────────────────


class WriteResult(BaseModel):
    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    review_required: bool = False
    pr_url: str | None = None
    reload: str | None = Field(
        default=None,
        description=(
            "How the change reaches the running process: 'hot' applies without a "
            "restart, 'roll' needs the pod to roll, 'restart' needs a manual one."
        ),
    )


class CreateInstanceRequest(BaseModel):
    instance: str
    values: dict[str, Any] = Field(
        default_factory=dict,
        description="Initial helm values as dot-path keys, merged over the defaults.",
    )


class FileSetSummary(BaseModel):
    name: str
    language: str
    suffixes: list[str]
    reload: str
    directory_setting: str


class AppSummary(BaseModel):
    service: str
    instance: str
    telemetry_name: str
    scale_deployed: bool
    multi_instance: bool
    file_sets: list[FileSetSummary]


class CatalogueEntry(BaseModel):
    service: str
    scale_deployed: bool
    multi_instance: bool
    file_sets: list[FileSetSummary]
    instances: list[str]


class ScalingResponse(BaseModel):
    supported: bool
    reason: str = ""
    deploy_target: str
    replica_count: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    keda_enabled: bool | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


class ScalingRequest(BaseModel):
    min_replicas: int | None = None
    max_replicas: int | None = None
    keda_enabled: bool | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


class FileSummary(BaseModel):
    name: str
    language: str
    size_bytes: int


class FileDetail(FileSummary):
    content: str


class FileWriteRequest(BaseModel):
    content: str


class StatusResponse(BaseModel):
    telemetry_name: str
    reporting: bool
    replicas: int
    last_seen_epoch: float | None = None
    started_epoch: float | None = None
    uptime_seconds: float | None = None


class MetricsResponse(BaseModel):
    telemetry_name: str
    window_seconds: int
    gauges: dict[str, float]
    rates: dict[str, float]


# ── shared plumbing ───────────────────────────────────────────


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _policy(request: Request) -> PolicyStore | None:
    return getattr(request.app.state, "policy_store", None)


def _forge(request: Request):
    return getattr(request.app.state, "forge", None)


def _deploy_target(request: Request) -> DeployTarget:
    return DeployTarget(request.app.state.settings.deployment.target)


def _resolve(service: str, instance: str) -> AppInstance:
    """Validate the identity, or map the failure to a 400/404."""
    try:
        return instances.instance_of(service, instance)
    except UnknownAppError:
        raise HTTPException(
            404, detail={"code": "unknown_app", "message": f"no such app: {service}"}
        ) from None
    except InvalidInstanceError as exc:
        raise HTTPException(400, detail={"code": "invalid_instance", "message": str(exc)}) from exc


def _overlay(gc: GitCrud, app: AppInstance) -> dict:
    try:
        return instances.read_overlay(gc, app)
    except ResourceNotFoundError:
        raise HTTPException(
            404,
            detail={
                "code": "not_deployed",
                "message": f"{app.service}/{app.instance} has no values overlay",
            },
        ) from None


def _enforce(request: Request, user: Any, name: str, changes: dict[str, Any]) -> bool:
    """Gate every path a write touches, and report whether any was protected.

    This layer writes whole documents rather than one dot-path at a time, so the
    per-path commit policy that ``api/v1/helm.py`` applies on the way in has to be
    applied explicitly here - otherwise a controller-owned field or a floating image
    ref would reach the deploy repo through the whole-document path.
    """
    for path, value in changes.items():
        try:
            validate_change(path, value)
        except CommitPolicyError as exc:
            raise HTTPException(
                403, detail={"code": "policy_violation", "message": str(exc)}
            ) from exc

    policy = _policy(request)
    if policy is None:
        return False
    override = authorize(
        user, "helmvars:override", role_config=request.app.state.role_config
    ).allowed
    protected = False
    for path in changes:
        if policy.is_protected(_CLASS, name, path):
            protected = True
        try:
            policy.enforce(_CLASS, name, path, override=override)
        except ProtectedVarError as exc:
            raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    return protected


def _commit(
    request: Request,
    user: Any,
    app: AppInstance,
    doc: dict,
    *,
    summary: str,
    protected: bool = False,
    if_match: str | None = None,
) -> WriteResult:
    """Write the whole overlay through the governed routing path."""
    gc = _gitcrud(request)
    settings = request.app.state.settings
    name = app.overlay_name
    # The commit standard caps the subject at 50 characters, and the scope here is
    # a service plus an instance, so the summary takes whatever room is left.
    scope = f"{app.service}/{app.instance}"
    message = build_message(
        CommitContext(
            ctype="cfg",
            scope=scope,
            summary=summary[: max(1, SUBJECT_MAX - len(f"cfg({scope}): "))],
            actor=user.user_id,
            role="helmvars:write",
            base_revision=if_match or "",
        )
    )

    def _write(branch: str):
        return gc.put(
            _CLASS, name, doc, user.user_id, message=message, base_revision=if_match, branch=branch
        )

    try:
        outcome = route_write(
            gc=gc,
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=_CLASS,
            resource=f"{_CLASS}/{name}",
            actor=user.user_id,
            protected=protected,
            title=f"cfg({scope}): {summary}",
            body=(
                f"Governed change to {app.service}/{app.instance} by {user.user_id}. "
                "Opened for review because production+team may not commit straight to main."
            ),
            write=_write,
        )
    except ConcurrencyConflictError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "conflict",
                "message": str(exc),
                "current": exc.current,
                "head": exc.head,
            },
        ) from exc
    except ReviewRequiredError as exc:
        raise HTTPException(409, detail={"code": "review_required", "message": str(exc)}) from exc

    audit_resource_change(user.user_id, "helmvars", name, "updated", {"summary": summary})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )


def _file_sets(service: str) -> list[FileSetSummary]:
    return [
        FileSetSummary(
            name=fs.name,
            language=fs.language,
            suffixes=list(fs.suffixes),
            reload=str(fs.reload),
            directory_setting=fs.dir_path,
        )
        for fs in catalogue.descriptor(service).files
    ]


# ── catalogue and lifecycle ───────────────────────────────────


@router.get("", dependencies=[_READ])
async def list_apps(user: CurrentUser, request: Request) -> list[CatalogueEntry]:
    """Every manageable app, with the instances currently deployed."""
    gc = _gitcrud(request)
    deployed = instances.list_instances(gc)
    return [
        CatalogueEntry(
            service=service,
            scale_deployed=catalogue.descriptor(service).scale_deployed,
            multi_instance=catalogue.descriptor(service).multi_instance,
            file_sets=_file_sets(service),
            instances=[i.instance for i in deployed if i.service == service],
        )
        for service in catalogue.services()
    ]


@router.post("/{service}/instances", response_model=WriteResult, dependencies=[_WRITE])
async def create_instance(
    service: str, body: CreateInstanceRequest, user: CurrentUser, request: Request
) -> WriteResult:
    """Deploy an instance by creating its values overlay.

    The overlay's presence is what the layer2-apps ApplicationSet turns into an Argo
    Application, so this is the whole deployment step.
    """
    app = _resolve(service, body.instance)
    gc = _gitcrud(request)
    if instances.exists(gc, app):
        raise HTTPException(
            409,
            detail={
                "code": "already_exists",
                "message": f"{service}/{body.instance} is already deployed",
            },
        )
    try:
        doc = instances.initial_overlay(app, body.values)
    except (InstanceExistsError, ValueError) as exc:
        raise HTTPException(400, detail={"code": "invalid_values", "message": str(exc)}) from exc
    protected = _enforce(request, user, app.overlay_name, body.values)
    return _commit(request, user, app, doc, summary="deploy instance", protected=protected)


@router.get("/{service}/{instance}", dependencies=[_READ])
async def get_app(service: str, instance: str, user: CurrentUser, request: Request) -> AppSummary:
    """One instance's identity and shape."""
    app = _resolve(service, instance)
    _overlay(_gitcrud(request), app)
    desc = catalogue.descriptor(service)
    return AppSummary(
        service=app.service,
        instance=app.instance,
        telemetry_name=app.telemetry_name,
        scale_deployed=desc.scale_deployed,
        multi_instance=desc.multi_instance,
        file_sets=_file_sets(service),
    )


@router.delete("/{service}/{instance}", response_model=WriteResult, dependencies=[_WRITE])
async def delete_instance(
    service: str, instance: str, user: CurrentUser, request: Request
) -> WriteResult:
    """Undeploy an instance by removing its values overlay."""
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    if not instances.exists(gc, app):
        raise HTTPException(
            404,
            detail={"code": "not_deployed", "message": f"{service}/{instance} is not deployed"},
        )
    settings = request.app.state.settings
    name = app.overlay_name
    scope = f"{app.service}/{app.instance}"
    message = build_message(
        CommitContext(
            ctype="cfg",
            scope=scope,
            summary="undeploy",
            actor=user.user_id,
            role="helmvars:write",
        )
    )

    def _write(branch: str):
        return gc.delete(_CLASS, name, user.user_id, message=message, branch=branch)

    try:
        outcome = route_write(
            gc=gc,
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=_CLASS,
            resource=f"{_CLASS}/{name}",
            actor=user.user_id,
            title=f"cfg({scope}): undeploy instance",
            body=f"Undeploy {service}/{instance} by {user.user_id}.",
            write=_write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(409, detail={"code": "review_required", "message": str(exc)}) from exc
    audit_resource_change(user.user_id, "helmvars", name, "deleted", {})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )


@router.get("/{service}/{instance}/values", dependencies=[_READ])
async def get_values(
    service: str, instance: str, user: CurrentUser, request: Request
) -> dict[str, Any]:
    """The instance's overlay document as stored."""
    app = _resolve(service, instance)
    return _overlay(_gitcrud(request), app)


# ── scaling ───────────────────────────────────────────────────


@router.get("/{service}/{instance}/scaling", dependencies=[_READ])
async def get_scaling(
    service: str, instance: str, user: CurrentUser, request: Request
) -> ScalingResponse:
    """The scaling dials, or why they do not apply here."""
    app = _resolve(service, instance)
    doc = _overlay(_gitcrud(request), app)
    target = _deploy_target(request)
    dials = scaling.read(doc, catalogue.descriptor(service), target)
    return ScalingResponse(deploy_target=str(target), **asdict(dials))


@router.put("/{service}/{instance}/scaling", response_model=WriteResult, dependencies=[_WRITE])
async def set_scaling(
    service: str,
    instance: str,
    body: ScalingRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Set the scaling dials. Refused when the deploy target has no such dials."""
    app = _resolve(service, instance)
    target = _deploy_target(request)
    supported, reason = scaling.support(catalogue.descriptor(service), target)
    if not supported:
        raise HTTPException(409, detail={"code": "scaling_unsupported", "message": reason})

    doc = _overlay(_gitcrud(request), app)
    try:
        changes = scaling.changes(doc, **body.model_dump(exclude_none=True))
    except InvalidDialError as exc:
        raise HTTPException(400, detail={"code": "invalid_dial", "message": str(exc)}) from exc
    if not changes:
        return WriteResult(changed=False)

    protected = _enforce(request, user, app.overlay_name, changes)
    for path, value in changes.items():
        set_path(doc, path, value)
    return _commit(
        request,
        user,
        app,
        doc,
        summary="set scaling dials",
        protected=protected,
        if_match=if_match,
    )


# ── consumed files ────────────────────────────────────────────


def _file_set(service: str, set_name: str):
    try:
        return catalogue.file_set(service, set_name)
    except UnknownAppError as exc:
        raise HTTPException(404, detail={"code": "unknown_file_set", "message": str(exc)}) from exc


@router.get("/{service}/{instance}/files/{set_name}", dependencies=[_READ])
async def list_app_files(
    service: str, instance: str, set_name: str, user: CurrentUser, request: Request
) -> list[FileSummary]:
    """Every file in the set, without their contents."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    doc = _overlay(_gitcrud(request), app)
    return [
        FileSummary(name=f.name, language=f.language, size_bytes=f.size_bytes)
        for f in files.list_files(doc, fs)
    ]


@router.get("/{service}/{instance}/files/{set_name}/{filename}", dependencies=[_READ])
async def read_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    user: CurrentUser,
    request: Request,
) -> FileDetail:
    """One file's content."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    doc = _overlay(_gitcrud(request), app)
    try:
        found = files.read_file(doc, fs, filename)
    except FileNotInSetError:
        raise HTTPException(
            404, detail={"code": "no_such_file", "message": f"{filename} is not in {set_name}"}
        ) from None
    return FileDetail(
        name=found.name,
        language=found.language,
        size_bytes=found.size_bytes,
        content=found.content,
    )


@router.put(
    "/{service}/{instance}/files/{set_name}/{filename}",
    response_model=WriteResult,
    dependencies=[_WRITE],
)
async def write_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    body: FileWriteRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Add or replace a file the app consumes."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    doc = _overlay(_gitcrud(request), app)
    try:
        changed = files.upsert_file(doc, fs, filename, body.content)
    except InvalidFilenameError as exc:
        raise HTTPException(400, detail={"code": "invalid_filename", "message": str(exc)}) from exc
    if not changed:
        return WriteResult(changed=False, reload=str(fs.reload))
    protected = _enforce(request, user, app.overlay_name, {fs.values_path: filename})
    result = _commit(
        request,
        user,
        app,
        doc,
        summary=f"set {filename}",
        protected=protected,
        if_match=if_match,
    )
    return result.model_copy(update={"reload": str(fs.reload)})


@router.delete(
    "/{service}/{instance}/files/{set_name}/{filename}",
    response_model=WriteResult,
    dependencies=[_WRITE],
)
async def delete_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    user: CurrentUser,
    request: Request,
) -> WriteResult:
    """Remove a file the app consumes."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    doc = _overlay(_gitcrud(request), app)
    try:
        files.delete_file(doc, fs, filename)
    except FileNotInSetError:
        raise HTTPException(
            404, detail={"code": "no_such_file", "message": f"{filename} is not in {set_name}"}
        ) from None
    protected = _enforce(request, user, app.overlay_name, {fs.values_path: filename})
    result = _commit(request, user, app, doc, summary=f"remove {filename}", protected=protected)
    return result.model_copy(update={"reload": str(fs.reload)})


# ── operational surface ───────────────────────────────────────


def _reader(request: Request, client: Any) -> OperationalReader:
    settings = request.app.state.settings
    return OperationalReader(client, settings.clickhouse.effective_data_database)


@router.get("/{service}/{instance}/status", dependencies=[_READ])
async def get_status(
    service: str, instance: str, user: CurrentUser, request: Request, client: ClickHouseClient
) -> StatusResponse:
    """Whether the instance is reporting telemetry, and since when."""
    app = _resolve(service, instance)
    try:
        status = _reader(request, client).status(app.telemetry_name)
    except MetricsUnavailableError as exc:
        raise HTTPException(
            503, detail={"code": "metrics_unavailable", "message": str(exc)}
        ) from exc
    return StatusResponse(
        telemetry_name=status.telemetry_name,
        reporting=status.reporting,
        replicas=status.replicas,
        last_seen_epoch=status.last_seen_epoch,
        started_epoch=status.started_epoch,
        uptime_seconds=status.uptime_seconds,
    )


@router.get("/{service}/{instance}/metrics", dependencies=[_READ])
async def get_metrics(
    service: str, instance: str, user: CurrentUser, request: Request, client: ClickHouseClient
) -> MetricsResponse:
    """Throughput, CPU, memory and saturation for the instance."""
    app = _resolve(service, instance)
    try:
        found = _reader(request, client).metrics(app.telemetry_name)
    except MetricsUnavailableError as exc:
        raise HTTPException(
            503, detail={"code": "metrics_unavailable", "message": str(exc)}
        ) from exc
    return MetricsResponse(
        telemetry_name=found.telemetry_name,
        window_seconds=found.window_seconds,
        gauges=found.gauges,
        rates=found.rates,
    )
