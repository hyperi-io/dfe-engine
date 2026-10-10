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
GET    /api/v1/apps/{service}/{instance}/values       the instance's overlay values, secrets masked
GET    /api/v1/apps/{service}/{instance}/config       every declared option, with provenance
PUT    /api/v1/apps/{service}/{instance}/config       write options, and custom env beside them
GET    /api/v1/apps/{service}/{instance}/scaling      the scaling dials
PUT    /api/v1/apps/{service}/{instance}/scaling      set the scaling dials
GET    /api/v1/apps/{service}/{instance}/files/{set}  list the files it consumes
GET    ...        /files/{set}/{filename}             read one
PUT    ...        /files/{set}/{filename}             write one
DELETE ...        /files/{set}/{filename}             remove one
POST   ...        /files/{set}/copy                   reuse them on another instance
GET    ...        /files/{set}/links                  where linked files came from, and drift
POST   ...        /files/{set}/link                   link a file to a library artefact
POST   ...        /files/{set}/relink                 re-resolve every link
GET    /api/v1/apps/{service}/{instance}/history      every change, and whether Argo has it
GET    /api/v1/apps/{service}/{instance}/status       is it reporting, since when
GET    /api/v1/apps/{service}/{instance}/metrics      throughput, cpu, memory
GET    ...        /metrics/series                     cpu and memory min/max/avg/p95 over time

Every mutation is a git commit into the deploy repo through the same gitcrud path
``api/v1/helm.py`` uses, so the protected-var policy, review routing and audit apply
unchanged, and every write reports the same state Argo later acts on.

Optimistic concurrency runs on the deploy repo's revision. Every read that backs a
write carries it as ``etag``; send it back as ``If-Match`` and the write is refused
with a 409 if anything has been committed since. It is ONE repo-wide value rather
than a per-resource one, because that is what the gitcrud guard compares against -
an instance's own newest commit goes stale the moment any other resource is written,
and would refuse a caller who had raced nobody.

RBAC follows the privilege each route actually exercises rather than one blanket
class: the overlay's contents are the ``helmvars`` resource, standing an instance up
or tearing it down is ``deployment:write`` / ``deployment:delete``, and the
operational surface is ``service:{service}:metrics:read`` - reading telemetry is not
the same privilege as reading configuration.
"""

import functools
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scalo.concurrency import run_blocking

from dfe_engine.api import body_limits
from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    Settings,
    SourceReg,
    get_source_registry,
    require_action,
)
from dfe_engine.api.errors import ErrorResponse, backend_failure
from dfe_engine.api.v1.app_contracts import read_contract
from dfe_engine.api.v1.helm import conflict_error
from dfe_engine.api.v1.sampler import check_sample_scope, sample_org_scope
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import (
    AppInstance,
    DeployTarget,
    FileNotInSetError,
    InvalidContentError,
    InvalidDialError,
    InvalidFilenameError,
    InvalidInstanceError,
    MetricsUnavailableError,
    OperationalReader,
    UnknownAppError,
    ValidationResult,
    appconfig,
    catalogue,
    contract,
    dryrun,
    files,
    instances,
    library,
    links,
    routing,
    scaling,
    validate,
)
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud
from dfe_engine.gitcrud.commit_policy import (
    CommitContext,
    CommitPolicyError,
    build_message,
    validate_result,
)
from dfe_engine.gitcrud.engine import ResourceNotFoundError, del_path, set_path
from dfe_engine.gitcrud.routing import ReviewRequiredError, route_write
from dfe_engine.governance import PolicyStore, ProtectedVarError
from dfe_engine.sampling import SampleMode, SampleRequest, SamplerError
from dfe_engine.source.flow import stage_instance_ceiling
from dfe_engine.source.registry import SourceNotFoundError

router = APIRouter(prefix="/apps", tags=["App Management"], dependencies=[WRITE_TURN])

_CLASS = instances.HELMVARS_CLASS

# The overlay's own contents are the helmvars resource, so they keep that class's
# actions. Standing an app up or tearing it down is a deployment action, not a var
# edit, and undeploy carries its own so it can be withheld separately.
_READ = Depends(require_action(scopes_dict["helmvars_read"]))
_WRITE = Depends(require_action(scopes_dict["helmvars_write"]))
_DEPLOY_READ = Depends(require_action(scopes_dict["deployment_read"]))
_DEPLOY_WRITE = Depends(require_action(scopes_dict["deployment_write"]))
_DEPLOY_DELETE = Depends(require_action(scopes_dict["deployment_delete"]))


# --- request and response models ---


class ValidationModel(BaseModel):
    status: str = Field(
        description=(
            "'valid' or 'invalid' when a backend answered, 'unavailable' when none "
            "could, 'disabled' when validation is off for this deployment."
        )
    )
    backend: str = ""
    message: str = ""
    errors: list[str] = Field(default_factory=list)


class WriteResult(BaseModel):
    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    review_required: bool = False
    pr_url: str | None = None
    validation: ValidationModel | None = None
    reload: str | None = Field(
        default=None,
        description=(
            "How the change reaches the running process: 'hot' applies without a "
            "restart, 'roll' needs the pod to roll, 'restart' needs a manual one."
        ),
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description=(
            "One command per app whose running process cannot take this change "
            "where it stands. Empty where the write was hot, or where a GitOps "
            "controller rolls the pod itself."
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


def _routing_flag() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        description=(
            "Whether this app's routing is compiled from the source definitions. "
            "False means the /routing routes answer 400 for every instance of it."
        )
    )


def _etag_field() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default=None,
        description=(
            "The deploy repo's revision when this was read. Send it back as the "
            "If-Match header on a write to have the write refused with a 409 if "
            "anything has been committed since. Repo-wide, not per resource."
        ),
    )


def _scope_field() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default="stack",
        description=(
            "stack: the routing compiles from every source into one deployment; "
            "instance: it compiles from the one source the instance is named for, "
            "and the engine deploys and removes such instances with their sources"
        ),
    )


def _optional_flag() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default=False,
        description=(
            "Whether a deployment runs without this app. Derived from default_in "
            "being empty, so it cannot disagree with it: an app nothing deploys by "
            "default is the one an operator turns on."
        ),
    )


def _profiles_field() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default_factory=list,
        description=(
            "The deployment profiles this app may be deployed in; empty means every "
            "profile. An offer to list, not a gate: the deploy repo decides what "
            "is actually deployed."
        ),
    )


def _default_in_field() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default=None,
        description=(
            "The profiles a deployment runs this app in without being asked. Null "
            "means the same as profiles; an empty list means nothing deploys it, "
            "which is what optional reports."
        ),
    )


def _idle_when_field() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default_factory=list,
        description=(
            "The app's own config dot-paths whose emptiness means it has no work, "
            "so an instance that is Ready and doing nothing can be reported as "
            "unconfigured rather than broken. Declared by the app, evaluated by the "
            "app: the engine reports these and never resolves them."
        ),
    )


def _display_name_field() -> Any:
    """A fresh field descriptor, since a FieldInfo belongs to one model."""
    return Field(
        default=None,
        description=(
            "The name a console shows a person for this app, from the app manifest. "
            "Null when the manifest names none, and the service id is the label. A "
            "label only: routes, charts and telemetry keep the service id."
        ),
    )


class AppSummary(BaseModel):
    service: str
    display_name: str | None = _display_name_field()
    instance: str
    telemetry_name: str
    scale_deployed: bool
    multiplicity: str
    has_compiled_routing: bool = _routing_flag()
    routing_scope: str = _scope_field()
    optional: bool = _optional_flag()
    profiles: list[str] = _profiles_field()
    default_in: list[str] | None = _default_in_field()
    idle_when: list[str] = _idle_when_field()
    file_sets: list[FileSetSummary]


class CatalogueEntry(BaseModel):
    service: str
    display_name: str | None = _display_name_field()
    scale_deployed: bool
    multiplicity: str
    has_compiled_routing: bool = _routing_flag()
    routing_scope: str = _scope_field()
    optional: bool = _optional_flag()
    profiles: list[str] = _profiles_field()
    default_in: list[str] | None = _default_in_field()
    offered: bool = Field(
        default=True,
        description=(
            "Whether this app is offered in the profile the caller asked about. True "
            "for every app when the caller named no profile, so a console that does "
            "not know the deployment's profile lists everything rather than hiding "
            "what it cannot rule out."
        ),
    )
    source_types: list[str] = Field(
        default_factory=list,
        description=(
            "The source families a source-bound instance of this app can poll; a "
            "fetcher-based source's fetcher.source_type must be one of them"
        ),
    )
    transform_engine: str | None = Field(
        default=None,
        description=(
            "The engine name a source writes in transform.engine to select this app, "
            "or null when the app is not a transform. Derived from the same catalogue "
            "rule the write path validates against, so a picker reading this field and "
            "the validator cannot disagree."
        ),
    )
    file_sets: list[FileSetSummary]
    instances: list[str]


class ValuesResponse(BaseModel):
    """The overlay document, wrapped so the revision has somewhere to live.

    The document is nested rather than returned bare: its keys are the chart's,
    so a top-level ``etag`` beside them would collide with any chart that ever
    names a value that.
    """

    values: dict[str, Any]
    etag: str | None = _etag_field()


class ConfigFieldModel(BaseModel):
    """One option the app declares, and what this instance would run for it."""

    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(description="The overlay path a write addresses, `config.` rooted")
    type: str
    title: str = ""
    description: str = ""
    secret: bool = Field(
        description="Credential material: neither its value nor its default is returned"
    )
    enum: list[Any] | None = Field(
        default=None, description="The values it accepts, where the app declares a closed set"
    )
    default: Any = Field(default=None, description="The app's own default, null for a secret")
    value: Any = Field(default=None, description="What this instance would run, null for a secret")
    provenance: str = Field(
        description="Where the value comes from: `overlay`, `default`, `chart` or `unset`"
    )
    dial: str | None = Field(default=None, description="Not resolved yet; always null")
    protected: bool = Field(description="Whether the protected-var policy covers this path")
    # `set` is the wire name; the attribute is named around the builtin.
    is_set: bool = Field(alias="set", description="Whether this instance has a value at all")


class UnknownFieldModel(BaseModel):
    """An overlay key under `config:` that the app's contract does not declare."""

    path: str
    value: Any = None


class CustomEnvModel(BaseModel):
    """An environment key written under `extraEnv`, which no contract declares."""

    path: str
    value: Any = None


class AppConfigResponse(BaseModel):
    """Every option an instance has, and the overlay keys none of them explain."""

    etag: str | None = _etag_field()
    available: bool = Field(
        description="False when this deployment has mounted no contract for the app"
    )
    fields: list[ConfigFieldModel] = Field(default_factory=list)
    unknown: list[UnknownFieldModel] = Field(default_factory=list)
    custom: list[CustomEnvModel] = Field(
        default_factory=list,
        description=(
            "Environment keys written under `extraEnv`. Reported apart from "
            "`unknown`, which is a config key that has outrun its contract; these "
            "are outside every contract on purpose."
        ),
    )


class ConfigWriteRequest(BaseModel):
    """Changes to one instance's config, each keyed by the path the GET reports."""

    changes: dict[str, Any] = Field(
        description=(
            "One entry per option. A `config.*` key is checked against the app's "
            "own schema before anything is committed; an `extraEnv.<NAME>` key is "
            "checked as an environment name only, and a null value there removes it."
        )
    )


class ConfigWriteResult(WriteResult):
    """A config write, and how any custom environment in it reaches the container."""

    custom_env: str = Field(
        default="",
        description=(
            "How the `extraEnv` keys in this write reach the app. Empty when the "
            "write carried none."
        ),
    )


class ScalingResponse(BaseModel):
    supported: bool
    reason: str = ""
    deploy_target: str
    etag: str | None = _etag_field()
    replica_count: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    keda_enabled: bool | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


class ScalingRequest(BaseModel):
    replica_count: int | None = Field(
        default=None,
        description=(
            "Fixed pod count, for a deployment with KEDA off. Refused unless "
            "`keda_enabled` is false in this request or already in the overlay: the "
            "chart omits `replicas` while KEDA is on, which it is by default, and the "
            "ScaledObject owns the count."
        ),
    )
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
    etag: str | None = _etag_field()


class FileWriteRequest(BaseModel):
    content: str


class RoutingResponse(BaseModel):
    """Source-derived routing: what it should be, and what the overlay carries."""

    compiler: str = Field(description="Manifest-declared compiler that derives these blocks")
    values_paths: dict[str, str] = Field(
        default_factory=dict,
        description="Each derived block by name, and the overlay dot-path it is written to",
    )
    drift: bool = Field(description="The overlay disagrees with the current sources")
    absent: bool = Field(
        description="The overlay carries no routing, so the app runs on built-in defaults"
    )
    compiled: dict[str, Any] = Field(
        default_factory=dict, description="Each block the sources call for now, by name"
    )
    deployed: dict[str, Any] = Field(
        default_factory=dict, description="The same blocks as the overlay carries them"
    )
    etag: str | None = _etag_field()


class DryRunRequest(BaseModel):
    """Run a file over sampled events without saving or deploying anything."""

    name: str = Field(description="File in the set to run")
    content: str | None = Field(
        default=None,
        description="Unsaved content to run instead of what is committed. Nothing is written.",
    )
    source: str = Field(
        default="",
        description="Source to sample from. Defaults to the instance, which for a "
        "source-bound app IS the source.",
    )
    limit: int = Field(
        default=10, ge=1, le=dryrun.MAX_EVENTS, description="Events to sample and run over"
    )


class DryRunEventModel(BaseModel):
    """What the program did to one event."""

    index: int
    before: str
    after: str = ""
    error: str = ""
    dropped: bool = False
    changed: bool = False


class DryRunResponse(BaseModel):
    """A dry run's per-event outcomes and totals."""

    status: str = Field(description="completed | unavailable | disabled | unsupported | failed")
    backend: str = ""
    message: str = ""
    source: str = ""
    sampled: int = 0
    succeeded: int = 0
    failed: int = 0
    dropped: int = 0
    truncated: bool = Field(
        default=False, description="Events were cut by the count or output-size ceiling"
    )
    events: list[DryRunEventModel] = Field(default_factory=list)


class CopyFilesRequest(BaseModel):
    target_instance: str = Field(
        description="Instance to copy into. For a source-bound app this is the source."
    )
    overwrite: bool = Field(
        default=False,
        description="Replace files of the same name in the target instead of refusing.",
    )


class CopyFilesResult(WriteResult):
    copied: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class LinkRequest(BaseModel):
    name: str = Field(description="Filename the artefact's content resolves into.")
    artifact: str
    version: int | None = Field(
        default=None, description="Pin this version. Omit for the artefact's current one."
    )
    tag: str = Field(
        default="",
        description="Follow this tag instead of a version; the resolved version is recorded.",
    )


class LinkModel(BaseModel):
    name: str
    artifact: str
    version: int
    digest: str
    tag: str = ""


class LinkStatusModel(LinkModel):
    resolved_digest: str = Field(description="Digest of the content sitting in the file set.")
    available_version: int | None = Field(
        default=None, description="The version re-resolving would move this link to."
    )
    missing: bool = Field(description="The artefact or its linked version is gone.")
    drift: bool = Field(description="The content here is not what the linked version holds.")
    outdated: bool = Field(description="The link's target has moved on since it resolved.")


class LinkResult(WriteResult):
    link: LinkModel | None = None


class RelinkResult(WriteResult):
    relinked: list[LinkModel] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    sha: str
    timestamp: int
    actor: str = ""
    summary: str = ""
    state: str = Field(
        description=(
            "'committed' when no Argo revision was supplied, otherwise 'applied' if "
            "the cluster has synced this commit or 'pending' if it has not yet."
        )
    )


class StatusResponse(BaseModel):
    telemetry_name: str
    reporting: bool
    last_seen_epoch: float | None = None
    started_epoch: float | None = None
    uptime_seconds: float | None = None


class ResourceBucketModel(BaseModel):
    metric: str
    bucket_epoch: float
    minimum: float
    maximum: float
    average: float
    p95: float
    samples: int


class ResourceSeriesResponse(BaseModel):
    telemetry_name: str
    window_seconds: int
    bucket_seconds: int
    buckets: list[ResourceBucketModel] = Field(
        description=(
            "One entry per metric per time bucket, aggregated across every pod "
            "reporting for this instance."
        )
    )


class MetricsResponse(BaseModel):
    telemetry_name: str
    window_seconds: int
    gauges: dict[str, float]
    rates: dict[str, float]


# --- shared plumbing ---


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _etag(gc: GitCrud) -> str | None:
    """The revision an If-Match write on this surface is checked against.

    Repo HEAD, because that is exactly what ``GitCrud._guard_revision`` compares
    a base revision to. The instance's own newest commit would be wrong: it goes
    stale as soon as any other resource in the deploy repo is written, and would
    then 409 a caller who had raced nobody.
    """
    return gc.head_revision()


def _policy(request: Request) -> PolicyStore | None:
    return getattr(request.app.state, "policy_store", None)


def _forge(request: Request):
    return getattr(request.app.state, "forge", None)


def _require_metrics_read(request: Request, user: Any, service: str) -> None:
    """Gate the operational surface on the per-service metrics action.

    Reading telemetry is a different privilege from reading configuration, so it
    binds to ``service:{service}:metrics:read`` - the per-service action the shipped
    roles already grant to infra_viewer and infra_admin.
    """
    action = f"service:{service}:metrics:read"
    if not authorize(user, action, role_config=request.app.state.role_config).allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": f"requires {action}"})


def _require_source(request: Request, app: AppInstance) -> None:
    """For a source-bound app, refuse an instance that names no defined source.

    The instance IS the source, and its topics are derived from that name, so a
    typo would otherwise deploy a transform consuming a topic nothing writes.

    The registry is resolved through the dependency that owns it rather than off
    ``app.state``, which never carries one -- reading it there made this guard
    unconditionally pass.
    """
    if not catalogue.descriptor(app.service).source_bound:
        return
    try:
        get_source_registry().get_source(app.instance)
    except SourceNotFoundError as exc:
        raise HTTPException(
            404,
            detail={
                "code": "unknown_source",
                "message": (
                    f"{app.service} instances are named for the source they transform, "
                    f"and no source {app.instance!r} is defined"
                ),
            },
        ) from exc


def _validate(request: Request, fs, content: str) -> ValidationResult:
    """Check authored content against this deployment's validation posture."""
    return validate(fs, content, enabled=request.app.state.settings.transform_validation.enabled)


def _validation_model(result: ValidationResult) -> ValidationModel:
    return ValidationModel(
        status=str(result.status),
        backend=result.backend,
        message=result.message,
        errors=list(result.errors),
    )


def _deploy_target(request: Request) -> DeployTarget:
    return DeployTarget(request.app.state.settings.deployment.target)


def _chart_deployed(request: Request) -> bool:
    """Whether a chart deploys the apps, which is what sets their deployment-owned paths."""
    return _deploy_target(request) is not DeployTarget.DOCKER


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


def _enforce(request: Request, user: Any, name: str, doc: dict) -> bool:
    """Gate every leaf the write adds, changes or drops, and say if any is protected.

    This layer writes whole documents rather than one dot-path at a time, so the
    commit-policy value rules run over the finished document here, as
    ``api/v1/helm.py`` runs them over the document a var write leaves behind. They
    read every leaf of the result, not the request keys: a caller supplying
    ``{"image": {"tag": "latest"}}`` nests the leaf out of sight of a check that
    only inspects what was sent. Only a violation the write adds is refused, so an
    overlay already carrying one still takes the write that repairs it. The
    protected-var gate compares against the stored overlay the same way: a locked
    leaf the write keeps needs no override, and one a smaller parent map drops does.
    """
    try:
        stored = _gitcrud(request).get(_CLASS, name)
    except ResourceNotFoundError:
        stored = {}
    try:
        validate_result(stored, doc, keda_by_default=scaling.keda_by_default(_CLASS, name))
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc

    policy = _policy(request)
    if policy is None:
        return False
    override = authorize(
        user, "helmvars:override", role_config=request.app.state.role_config
    ).allowed
    try:
        return policy.enforce_document(_CLASS, name, stored, doc, override=override)
    except ProtectedVarError as exc:
        raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc


def commit_overlay(
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
    scope, subject_summary = instances.fit_subject(app, summary)
    message = build_message(
        CommitContext(
            ctype="cfg",
            scope=scope,
            summary=subject_summary,
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
        raise conflict_error(exc) from exc
    except ReviewRequiredError as exc:
        raise HTTPException(409, detail={"code": "review_required", "message": str(exc)}) from exc

    audit_resource_change(user.user_id, "helmvars", name, "updated", {"summary": summary})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
        restart_required=appconfig.render_and_report(gc, settings),
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


# --- catalogue and lifecycle ---


@router.get("", dependencies=[_DEPLOY_READ])
def list_apps(
    user: CurrentUser,
    request: Request,
    settings: Settings,
    profile: str = Query(
        default="",
        description=(
            "Deployment profile to judge each app's offer against, so the rule stays "
            "in one place rather than being re-derived by every caller. Omit it and "
            "this deployment's own profile is used; omit it where the deployment "
            "states none and every app is reported as offered."
        ),
    ),
) -> list[CatalogueEntry]:
    """Every manageable app, with the instances currently deployed."""
    gc = _gitcrud(request)
    deployed = instances.list_instances(gc)
    # A caller naming no profile means this one, which the deployer injected.
    judged_against = profile or settings.deployment.profile
    entries: list[CatalogueEntry] = []
    for service in catalogue.services():
        desc = catalogue.descriptor(service)
        entries.append(
            CatalogueEntry(
                service=service,
                display_name=desc.display_name or None,
                scale_deployed=desc.scale_deployed,
                multiplicity=str(desc.multiplicity),
                has_compiled_routing=desc.has_compiled_routing,
                routing_scope=str(desc.routing_scope),
                optional=desc.optional,
                profiles=sorted(desc.profiles),
                default_in=None if desc.default_in is None else sorted(desc.default_in),
                offered=desc.offered_in(judged_against),
                source_types=list(desc.source_types),
                transform_engine=desc.transform_engine or None,
                file_sets=_file_sets(service),
                instances=[i.instance for i in deployed if i.service == service],
            )
        )
    return entries


@router.post("/{service}/instances", response_model=WriteResult, dependencies=[_DEPLOY_WRITE])
def create_instance(
    service: str,
    body: CreateInstanceRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Deploy an instance by creating its values overlay.

    The overlay's presence is what the layer2-apps ApplicationSet turns into an Argo
    Application, so this is the whole deployment step. A new instance stores nothing,
    so a masked value copied from another instance's read is a 400 ``masked_value``.
    """
    app = _resolve(service, body.instance)
    values = _restored({}, body.values)
    gc = _gitcrud(request)
    if instances.exists(gc, app):
        raise HTTPException(
            409,
            detail={
                "code": "already_exists",
                "message": f"{service}/{body.instance} is already deployed",
            },
        )
    ceiling = stage_instance_ceiling(app.descriptor, request.app.state.settings)
    allowed, reason = instances.additional_instance_allowed(gc, app, ceiling)
    if not allowed:
        raise HTTPException(409, detail={"code": "single_instance_app", "message": reason})
    _require_source(request, app)
    try:
        doc = instances.initial_overlay(app, values)
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_values", "message": str(exc)}) from exc
    # An instance-routed app is derived state: it exists for an active, deployed
    # source and carries that source's compiled block from the first commit.
    if app.descriptor.routing_is_per_instance:
        registry = get_source_registry()
        source = registry.get_source(app.instance)
        if source.state != "active" or not source.deployed_version:
            raise HTTPException(
                409,
                detail={
                    "code": "source_not_live",
                    "message": (
                        f"{service} instances follow their source: {app.instance!r} must be "
                        "active and deployed, and the source deploy creates the instance"
                    ),
                },
            )
        found = _routing_status(request, app, doc, registry)
        routing.apply(app.descriptor, doc, found.compiled)
    protected = _enforce(request, user, app.overlay_name, doc)
    return commit_overlay(
        request,
        user,
        app,
        doc,
        summary="deploy instance",
        protected=protected,
        if_match=if_match,
    )


@router.get("/{service}/{instance}", dependencies=[_DEPLOY_READ])
def get_app(service: str, instance: str, user: CurrentUser, request: Request) -> AppSummary:
    """One instance's identity and shape."""
    app = _resolve(service, instance)
    _overlay(_gitcrud(request), app)
    desc = catalogue.descriptor(service)
    return AppSummary(
        service=app.service,
        display_name=desc.display_name or None,
        instance=app.instance,
        telemetry_name=app.telemetry_name,
        scale_deployed=desc.scale_deployed,
        multiplicity=str(desc.multiplicity),
        has_compiled_routing=desc.has_compiled_routing,
        routing_scope=str(desc.routing_scope),
        optional=desc.optional,
        profiles=sorted(desc.profiles),
        default_in=None if desc.default_in is None else sorted(desc.default_in),
        idle_when=list(desc.idle_when),
        file_sets=_file_sets(service),
    )


@router.delete("/{service}/{instance}", response_model=WriteResult, dependencies=[_DEPLOY_DELETE])
def delete_instance(
    service: str,
    instance: str,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Undeploy an instance by removing its values overlay."""
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    if not instances.exists(gc, app):
        raise HTTPException(
            404,
            detail={"code": "not_deployed", "message": f"{service}/{instance} is not deployed"},
        )
    return remove_overlay(request, user, app, if_match=if_match)


def remove_overlay(
    request: Request,
    user: Any,
    app: AppInstance,
    *,
    if_match: str | None = None,
) -> WriteResult:
    """Delete the whole overlay through the governed routing path."""
    gc = _gitcrud(request)
    settings = request.app.state.settings
    service, instance = app.service, app.instance
    name = app.overlay_name
    scope, subject_summary = instances.fit_subject(app, "undeploy")
    message = build_message(
        CommitContext(
            ctype="cfg",
            scope=scope,
            summary=subject_summary,
            actor=user.user_id,
            role="helmvars:write",
        )
    )

    def _write(branch: str):
        return gc.delete(
            _CLASS, name, user.user_id, message=message, base_revision=if_match, branch=branch
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
            title=f"cfg({scope}): undeploy instance",
            body=f"Undeploy {service}/{instance} by {user.user_id}.",
            write=_write,
        )
    except ConcurrencyConflictError as exc:
        raise conflict_error(exc) from exc
    except ReviewRequiredError as exc:
        raise HTTPException(409, detail={"code": "review_required", "message": str(exc)}) from exc
    audit_resource_change(user.user_id, "helmvars", name, "deleted", {})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
        restart_required=appconfig.render_and_report(gc, settings),
    )


@router.get("/{service}/{instance}/history", dependencies=[_READ])
def get_history(
    service: str,
    instance: str,
    user: CurrentUser,
    request: Request,
    applied_revision: str | None = None,
    limit: int = 20,
) -> list[HistoryEntry]:
    """Every change to this instance, newest first.

    Supply the revision Argo has synced as ``applied_revision`` to see which
    commits have reached the cluster and which are still pending.
    """
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    _overlay(gc, app)
    entries = instances.history(
        gc, app, applied_revision=applied_revision, limit=min(max(limit, 1), 200)
    )
    return [
        HistoryEntry(
            sha=e.sha,
            timestamp=e.timestamp,
            actor=e.actor,
            summary=e.summary,
            state=e.state,
        )
        for e in entries
    ]


@router.get("/{service}/{instance}/values", dependencies=[_READ])
def get_values(service: str, instance: str, user: CurrentUser, request: Request) -> ValuesResponse:
    """The instance's overlay document, credentials masked, with the revision to write against.

    Masked as the config route hides them: by the app's own secret marker in its
    schema, and by name wherever the schema says nothing.
    """
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    return ValuesResponse(
        values=contract.redact_overlay(read_contract(service), doc), etag=_etag(gc)
    )


@router.get("/{service}/{instance}/config", dependencies=[_READ])
def get_app_config(
    service: str, instance: str, user: CurrentUser, request: Request
) -> AppConfigResponse:
    """Every option the app declares, with this instance's value and where it comes from.

    The overlay alone cannot answer this: it holds only what was written, so an
    option nobody has touched is invisible in it. The app's own contract supplies
    the rest, which is what lets the console show an untouched option with the
    default it would run rather than an empty box.
    """
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    found = read_contract(service)
    policy = _policy(request)
    name = app.overlay_name

    def _protected(path: str) -> bool:
        return policy.is_protected(_CLASS, name, path) if policy is not None else False

    view = contract.resolve_config(
        found, doc, is_protected=_protected, chart_deployed=_chart_deployed(request)
    )
    return AppConfigResponse(
        etag=_etag(gc),
        available=view.available,
        fields=[
            ConfigFieldModel(
                path=f.path,
                type=f.type,
                title=f.title,
                description=f.description,
                secret=f.secret,
                enum=f.enum,
                default=f.default,
                value=f.value,
                provenance=str(f.provenance),
                dial=f.dial,
                protected=f.protected,
                is_set=f.is_set,
            )
            for f in view.fields
        ],
        unknown=[UnknownFieldModel(path=u.path, value=u.value) for u in view.unknown],
        custom=[CustomEnvModel(path=c.path, value=c.value) for c in view.custom],
    )


def _require_fresh(gc: GitCrud, if_match: str | None) -> None:
    """Refuse a write whose caller read against a revision the repo has moved past.

    412 rather than the 409 a commit-time race answers with, because nothing has
    been attempted yet. The guard inside the commit still answers 409 for anything
    landing between this check and the write.
    """
    if if_match and if_match != _etag(gc):
        raise HTTPException(
            412,
            detail={
                "code": "stale_etag",
                "message": "the deploy repo has moved on since this was read",
                "head": _etag(gc),
            },
        )


def _refuse(status: int, code: str, path: str, message: str) -> HTTPException:
    """One refusal shape, so the console can show the path beside the reason."""
    return HTTPException(status, detail={"code": code, "path": path, "message": message})


def _table_name_taken(exc: files.TableNameTakenError) -> HTTPException:
    """A 409 naming both files, since either one can be renamed to resolve it."""
    return HTTPException(
        409,
        detail={
            "code": "table_name_taken",
            "message": str(exc),
            "files": [exc.existing, exc.name],
        },
    )


def _checked_changes(
    service: str,
    found: contract.AppContract,
    changes: dict[str, Any],
    doc: dict,
    *,
    chart_deployed: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a request into config paths and environment keys, refusing what the app would.

    Every refusal happens here, before the overlay document is touched at all, so
    a rejected write leaves the deploy repo on the revision the caller read.
    ``doc`` is the overlay as read, which decides what its chart sets, and
    ``chart_deployed`` whether a chart sets anything at all.

    A `config.*` path the contract does not declare is carried through unchecked:
    there is nothing to check it against, and the read route already reports such
    a key as unknown rather than pretending it is not there.
    """
    options = contract.declared_options(found)
    config_changes: dict[str, Any] = {}
    env_changes: dict[str, Any] = {}
    for path, value in changes.items():
        if path.startswith(f"{appconfig.ENV_ROOT}."):
            key = path.split(".", 1)[1]
            if not contract.ENV_NAME.match(key):
                raise _refuse(
                    400,
                    "invalid_env_name",
                    path,
                    f"{key!r} is not an environment name: upper case, digits and "
                    "underscores, starting with a letter",
                )
            reason = contract.check_env_value(value)
            if reason:
                raise _refuse(400, "invalid_env_value", path, reason)
            names = contract.chart_env_names(service, doc, chart_deployed=chart_deployed)
            decides = names.get(key)
            if decides:
                raise _refuse(
                    409,
                    "chart_set_env",
                    path,
                    f"the {service} deployment sets {key} itself, for {decides}. Change "
                    "it where the deployment sets it, not in one instance's overlay",
                )
            env_changes[key] = value
            continue
        if not path.startswith(f"{appconfig.CONFIG_ROOT}."):
            raise _refuse(
                400,
                "invalid_path",
                path,
                f"a change addresses {appconfig.CONFIG_ROOT}.<option> or "
                f"{appconfig.ENV_ROOT}.<NAME>",
            )
        supplier = contract.chart_supplier(service, path, doc, chart_deployed=chart_deployed)
        if supplier:
            raise _refuse(
                409,
                "chart_derived",
                path,
                f"the deployment decides this through {supplier}. Change it there, "
                "not in one instance's overlay",
            )
        option = options.get(path)
        if option is not None:
            reason = contract.check_value(option, value)
            if reason:
                raise _refuse(400, "invalid_value", path, reason)
        config_changes[path] = value
    return config_changes, env_changes


def _restored(doc: dict, changes: dict[str, Any]) -> dict[str, Any]:
    """The requested changes with every masked credential put back to what is stored."""
    out: dict[str, Any] = {}
    for path, value in changes.items():
        try:
            out[path] = contract.restore_masked_at(doc, path, value)
        except contract.MaskedValueError as exc:
            raise _refuse(400, exc.code, path, str(exc)) from exc
    return out


def _custom_env_delivery(request: Request, env_changes: dict[str, Any]) -> str:
    """How the custom environment in this write reaches the app's container."""
    if not env_changes:
        return ""
    if appconfig.custom_env_dir(request.app.state.settings) is None:
        return "the app's own chart renders extraEnv onto the container"
    return (
        "written to this deployment's app environment directory; the command in "
        "restart_required applies it"
    )


def _reload_mode(request: Request, result: WriteResult) -> str:
    """How this write reaches the running process, in the response's own vocabulary.

    A chart rolls the pod on any config change. A Compose stack has no controller
    to do that, so what it reports is whether a command was handed back.
    """
    if _deploy_target(request) is DeployTarget.KUBERNETES:
        return "roll"
    return "restart" if result.restart_required else "hot"


@router.put("/{service}/{instance}/config", response_model=ConfigWriteResult, dependencies=[_WRITE])
def set_app_config(
    service: str,
    instance: str,
    body: ConfigWriteRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> ConfigWriteResult:
    """Write options the app declares, and environment keys it does not.

    Judged against the app's own contract before anything is committed, so a value
    the app would refuse at startup leaves the deploy repo where it was rather than
    landing a commit an operator then has to revert.

    A secret is written like any other option: it goes into the overlay as the rest
    of this surface writes one, and neither this response nor a read route on this
    surface says what it is. A masked value written back as it was read keeps the
    stored credential; the mask where nothing is stored is a 400 ``masked_value``.
    A changed field beside a stored credential, in the same mapping or with the
    credential one mapping down (``brokers`` beside ``sasl.password``), is a 400
    ``credential_reentry_required`` until that mapping is written whole with its
    credentials typed again. A setting inside another mapping beside it, such as
    ``producer.retries``, is not. ``config`` and ``extraEnv`` are roots, so a
    setting directly under either never needs one.

    409 where the deployment already decides the value: a config path the chart
    derives, or an `extraEnv` name the chart sets for this app.
    """
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    _require_fresh(gc, if_match)
    config_changes, env_changes = _checked_changes(
        service,
        read_contract(service),
        _restored(doc, body.changes),
        doc,
        chart_deployed=_chart_deployed(request),
    )
    if not config_changes and not env_changes:
        return ConfigWriteResult(changed=False)

    for path, value in config_changes.items():
        set_path(doc, path, value)
    for key, value in env_changes.items():
        path = f"{appconfig.ENV_ROOT}.{key}"
        # A null removes the key rather than exporting an empty one.
        if value is None:
            del_path(doc, path)
        else:
            set_path(doc, path, value)

    protected = _enforce(request, user, app.overlay_name, doc)
    result = commit_overlay(
        request,
        user,
        app,
        doc,
        summary="set config",
        protected=protected,
        if_match=if_match,
    )
    return ConfigWriteResult(
        **result.model_dump(exclude={"reload", "validation"}),
        reload=_reload_mode(request, result),
        custom_env=_custom_env_delivery(request, env_changes),
    )


# --- scaling ---


@router.get("/{service}/{instance}/scaling", dependencies=[_READ])
def get_scaling(
    service: str, instance: str, user: CurrentUser, request: Request
) -> ScalingResponse:
    """The scaling dials, or why they do not apply here."""
    app = _resolve(service, instance)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    target = _deploy_target(request)
    dials = scaling.read(doc, catalogue.descriptor(service), target)
    return ScalingResponse(deploy_target=str(target), etag=_etag(gc), **asdict(dials))


@router.put("/{service}/{instance}/scaling", response_model=WriteResult, dependencies=[_WRITE])
def set_scaling(
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
        changes = scaling.changes(
            doc,
            keda_by_default=scaling.keda_by_default(_CLASS, app.overlay_name),
            **body.model_dump(exclude_none=True),
        )
    except InvalidDialError as exc:
        raise HTTPException(400, detail={"code": "invalid_dial", "message": str(exc)}) from exc
    if not changes:
        return WriteResult(changed=False)

    for path, value in changes.items():
        set_path(doc, path, value)
    protected = _enforce(request, user, app.overlay_name, doc)
    return commit_overlay(
        request,
        user,
        app,
        doc,
        summary="set scaling dials",
        protected=protected,
        if_match=if_match,
    )


# --- consumed files ---


def _file_set(service: str, set_name: str):
    try:
        return catalogue.file_set(service, set_name)
    except UnknownAppError as exc:
        raise HTTPException(404, detail={"code": "unknown_file_set", "message": str(exc)}) from exc


@router.get("/{service}/{instance}/files/{set_name}", dependencies=[_READ])
def list_app_files(
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


def _require_library_read(request: Request, user: Any) -> None:
    """Gate a route that reads the artefact library on the library's own grant.

    Linking resolves content out of the library, which is a different resource
    from the overlay the resolved content lands in.
    """
    action = f"{links.LIBRARY_CLASS}:read"
    if not authorize(user, action, role_config=request.app.state.role_config).allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": f"requires {action}"})


def _link_model(link: links.Link) -> LinkModel:
    return LinkModel(
        name=link.name,
        artifact=link.artifact,
        version=link.version,
        digest=link.digest,
        tag=link.tag,
    )


@router.get("/{service}/{instance}/files/{set_name}/links", dependencies=[_READ])
def list_app_links(
    service: str, instance: str, set_name: str, user: CurrentUser, request: Request
) -> list[LinkStatusModel]:
    """Where each linked file came from, and whether it still matches.

    ``drift`` means the content beside the link is no longer what the linked
    version holds - a local edit over a linked file. ``outdated`` means the link
    resolved cleanly but its target has moved since.
    """
    app = _resolve(service, instance)
    _require_library_read(request, user)
    fs = _file_set(service, set_name)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    try:
        checked = links.status(doc, fs, links.crud_source(gc))
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_links", "message": str(exc)}) from exc
    return [
        LinkStatusModel(
            **_link_model(s.link).model_dump(),
            resolved_digest=s.resolved_digest,
            available_version=s.available_version,
            missing=s.missing,
            drift=s.drift,
            outdated=s.outdated,
        )
        for s in checked
    ]


@router.post(
    "/{service}/{instance}/files/{set_name}/link",
    response_model=LinkResult,
    dependencies=[_WRITE],
)
def link_app_file(
    service: str,
    instance: str,
    set_name: str,
    body: LinkRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> LinkResult:
    """Link a file in the set to a library artefact.

    The artefact's content is resolved into the file set, because a chart can only
    render what is already in the values, and the provenance is recorded beside it
    so the link is recoverable.
    """
    app = _resolve(service, instance)
    _require_library_read(request, user)
    fs = _file_set(service, set_name)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    source = links.crud_source(gc)
    env = source.envelope(body.artifact)
    if env is None:
        raise HTTPException(
            404,
            detail={"code": "no_such_artifact", "message": f"no artefact {body.artifact!r}"},
        )
    try:
        changed = links.resolve(
            doc,
            fs,
            name=body.name,
            artifact=body.artifact,
            env=env,
            source=source,
            version=body.version,
            tag=body.tag,
        )
    except (links.ArtifactNotLinkableError, InvalidFilenameError) as exc:
        raise HTTPException(400, detail={"code": "not_linkable", "message": str(exc)}) from exc
    except files.TableNameTakenError as exc:
        raise _table_name_taken(exc) from exc
    except library.TagNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "no_such_tag", "message": f"no tag {exc.args[0]!r}"}
        ) from None
    except library.VersionNotFoundError as exc:
        raise HTTPException(
            404, detail={"code": "no_such_version", "message": f"no version {exc.args[0]}"}
        ) from None

    link = _link_model(links.read_link(doc, fs, body.name))
    if not changed:
        return LinkResult(changed=False, reload=str(fs.reload), link=link)
    protected = _enforce(request, user, app.overlay_name, doc)
    result = commit_overlay(
        request,
        user,
        app,
        doc,
        summary=f"link {body.name}",
        protected=protected,
        if_match=if_match,
    )
    return LinkResult(
        **result.model_dump(exclude={"reload", "validation"}),
        reload=str(fs.reload),
        link=link,
    )


@router.post(
    "/{service}/{instance}/files/{set_name}/relink",
    response_model=RelinkResult,
    dependencies=[_WRITE],
)
def relink_app_files(
    service: str,
    instance: str,
    set_name: str,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> RelinkResult:
    """Re-resolve every link in the set to what its target now names.

    A tag link follows its tag; a version-pinned link advances to the artefact's
    current version. This is the fix-once-roll-everywhere half of the library.
    """
    app = _resolve(service, instance)
    _require_library_read(request, user)
    fs = _file_set(service, set_name)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    try:
        moved = links.relink(doc, fs, links.crud_source(gc))
    except ValueError as exc:
        raise HTTPException(400, detail={"code": "invalid_links", "message": str(exc)}) from exc
    if not moved:
        return RelinkResult(changed=False, reload=str(fs.reload))
    protected = _enforce(request, user, app.overlay_name, doc)
    result = commit_overlay(
        request,
        user,
        app,
        doc,
        summary=f"relink {set_name}",
        protected=protected,
        if_match=if_match,
    )
    return RelinkResult(
        **result.model_dump(exclude={"reload", "validation"}),
        reload=str(fs.reload),
        relinked=[_link_model(link) for link in moved],
    )


@router.post(
    "/{service}/{instance}/files/{set_name}/copy",
    response_model=CopyFilesResult,
    dependencies=[_WRITE],
)
def copy_app_files(
    service: str,
    instance: str,
    set_name: str,
    body: CopyFilesRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> CopyFilesResult:
    """Copy this instance's authored files onto another instance of the same app.

    A transform is written against one source; reusing it on another should not mean
    retyping it. Only the files move - the target keeps its own source binding,
    scaling and identity.
    """
    source_app = _resolve(service, instance)
    target_app = _resolve(service, body.target_instance)
    if source_app == target_app:
        raise HTTPException(
            400, detail={"code": "same_instance", "message": "source and target are the same"}
        )
    fs = _file_set(service, set_name)
    gc = _gitcrud(request)
    source_doc = _overlay(gc, source_app)
    target_doc = _overlay(gc, target_app)

    existing = {f.name for f in files.list_files(target_doc, fs)}
    copied: list[str] = []
    skipped: list[str] = []
    for candidate in files.list_files(source_doc, fs):
        if candidate.name in existing and not body.overwrite:
            skipped.append(candidate.name)
            continue
        try:
            files.upsert_file(target_doc, fs, candidate.name, candidate.content)
        except files.TableNameTakenError as exc:
            raise _table_name_taken(exc) from exc
        copied.append(candidate.name)

    if not copied:
        return CopyFilesResult(changed=False, reload=str(fs.reload), copied=[], skipped=skipped)

    protected = _enforce(request, user, target_app.overlay_name, target_doc)
    result = commit_overlay(
        request,
        user,
        target_app,
        target_doc,
        summary=f"copy {set_name} from {instance}",
        protected=protected,
        if_match=if_match,
    )
    return CopyFilesResult(
        **result.model_dump(exclude={"reload", "validation"}),
        reload=str(fs.reload),
        copied=copied,
        skipped=skipped,
    )


@router.get("/{service}/{instance}/files/{set_name}/{filename}", dependencies=[_READ])
def read_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    user: CurrentUser,
    request: Request,
) -> FileDetail:
    """One file's content, with the revision to write it back against."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
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
        etag=_etag(gc),
    )


_JSON_ESCAPE_BYTES = 6
"""JSON may spell any character as a six-byte ``\\uXXXX`` escape."""

_WRITE_ENVELOPE_BYTES = 1024
"""The ``{"content": ...}`` wrapper around a file, with room for whitespace."""


async def _read_file_write(request: Request) -> FileWriteRequest:
    """The write's body, refused with 413 before a file over the object cap is buffered.

    The stream bound allows every character to arrive escaped, so a file that fits
    is never refused for its encoding; the content is then measured exactly.
    """
    limit = request.app.state.settings.repository.max_object_bytes
    raw = await body_limits.read_body_capped(
        request,
        limit=limit * _JSON_ESCAPE_BYTES + _WRITE_ENVELOPE_BYTES,
        code="payload_too_large",
        message=f"request body is larger than a file of {limit} bytes can be sent in "
        "(repository.max_object_bytes)",
    )
    try:
        body = FileWriteRequest.model_validate_json(raw)
    except ValidationError as exc:
        errors = [{**e, "loc": ("body", *e["loc"])} for e in exc.errors(include_url=False)]
        raise RequestValidationError(errors) from exc
    size = len(body.content.encode("utf-8"))
    if size > limit:
        raise body_limits.too_large(
            "payload_too_large",
            f"file is {size} bytes, over repository.max_object_bytes ({limit} bytes)",
        )
    return body


@router.put(
    "/{service}/{instance}/files/{set_name}/{filename}",
    response_model=WriteResult,
    dependencies=[_WRITE],
    responses={
        413: {
            "model": ErrorResponse,
            "description": (
                "The file exceeds repository.max_object_bytes (HTTP 413, code "
                "payload_too_large). Tune via DFE_REPOSITORY_MAX_OBJECT_BYTES."
            ),
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": FileWriteRequest.model_json_schema()}},
        }
    },
)
async def write_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Add or replace a file the app consumes, up to ``repository.max_object_bytes``.

    The file lands in the deploy repo's history for good, so an oversized one is
    refused before it is buffered rather than after it is committed.
    """
    body = await _read_file_write(request)
    return await run_blocking(
        functools.partial(
            _write_app_file, service, instance, set_name, filename, user, request, if_match, body
        )
    )


def _write_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    user: Any,
    request: Request,
    if_match: str | None,
    body: FileWriteRequest,
) -> WriteResult:
    """The deploy-repo half of ``write_app_file``, run on a worker thread."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    doc = _overlay(_gitcrud(request), app)

    checked = _validate(request, fs, body.content)
    if checked.blocks_write and request.app.state.settings.transform_validation.blocking:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_transform",
                "message": checked.message,
                "errors": list(checked.errors),
                "backend": checked.backend,
            },
        )

    try:
        changed = files.upsert_file(doc, fs, filename, body.content)
    except InvalidFilenameError as exc:
        raise HTTPException(400, detail={"code": "invalid_filename", "message": str(exc)}) from exc
    except InvalidContentError as exc:
        raise HTTPException(400, detail={"code": "invalid_content", "message": str(exc)}) from exc
    except files.TableNameTakenError as exc:
        raise _table_name_taken(exc) from exc
    reported = _validation_model(checked)
    if not changed:
        return WriteResult(changed=False, reload=str(fs.reload), validation=reported)
    protected = _enforce(request, user, app.overlay_name, doc)
    result = commit_overlay(
        request,
        user,
        app,
        doc,
        summary=f"set {filename}",
        protected=protected,
        if_match=if_match,
    )
    return result.model_copy(update={"reload": str(fs.reload), "validation": reported})


@router.delete(
    "/{service}/{instance}/files/{set_name}/{filename}",
    response_model=WriteResult,
    dependencies=[_WRITE],
)
def delete_app_file(
    service: str,
    instance: str,
    set_name: str,
    filename: str,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Remove a file the app consumes, and any link that produced it."""
    app = _resolve(service, instance)
    fs = _file_set(service, set_name)
    doc = _overlay(_gitcrud(request), app)
    try:
        files.delete_file(doc, fs, filename)
    except FileNotInSetError:
        raise HTTPException(
            404, detail={"code": "no_such_file", "message": f"{filename} is not in {set_name}"}
        ) from None
    # Provenance for a file that is gone would be re-resolved by the next relink,
    # putting the deleted file back.
    try:
        links.remove_link(doc, fs, filename)
    except links.LinkNotFoundError:
        pass
    protected = _enforce(request, user, app.overlay_name, doc)
    result = commit_overlay(
        request,
        user,
        app,
        doc,
        summary=f"remove {filename}",
        protected=protected,
        if_match=if_match,
    )
    return result.model_copy(update={"reload": str(fs.reload)})


# --- source-derived routing ---


def _routing_app(service: str, instance: str) -> AppInstance:
    app = _resolve(service, instance)
    if not app.descriptor.has_compiled_routing:
        raise HTTPException(
            400,
            detail={
                "code": "routing_not_compiled",
                "message": f"{service} routing is not derived from the source definitions",
            },
        )
    return app


def _routing_status(request: Request, app: AppInstance, doc: dict, source_registry: Any):
    try:
        return routing.status(
            app.descriptor,
            doc,
            source_registry,
            request.app.state.settings,
            instance=app.instance,
        )
    except routing.RoutingNotApplicableError as exc:
        raise HTTPException(
            409,
            detail={"code": "routing_not_applicable", "message": str(exc)},
        ) from exc
    except routing.UnknownRoutingCompilerError as exc:
        raise HTTPException(
            500,
            detail={
                "code": "unknown_routing_compiler",
                "message": f"the app manifest names compiler {exc.args[0]!r}, which this "
                f"engine does not implement (known: {', '.join(routing.compilers())})",
            },
        ) from None


@router.get("/{service}/{instance}/routing", response_model=RoutingResponse, dependencies=[_READ])
def get_app_routing(
    service: str,
    instance: str,
    user: CurrentUser,
    request: Request,
    source_registry: SourceReg,
) -> RoutingResponse:
    """What the sources compile to, against what the overlay actually carries.

    An absent block is called out separately from drift: it means the app is
    running on its built-in defaults, which is how a receiver silently ignores
    every source rule ever defined.
    """
    app = _routing_app(service, instance)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    found = _routing_status(request, app, doc, source_registry)
    return RoutingResponse(
        compiler=found.compiler,
        values_paths=found.values_paths,
        drift=found.drift,
        absent=found.absent,
        compiled=found.compiled,
        deployed=found.deployed,
        etag=_etag(gc),
    )


@router.post(
    "/{service}/{instance}/routing/sync", response_model=WriteResult, dependencies=[_WRITE]
)
def sync_app_routing(
    service: str,
    instance: str,
    user: CurrentUser,
    request: Request,
    source_registry: SourceReg,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Rewrite the overlay's routing to what the sources currently compile to."""
    app = _routing_app(service, instance)
    gc = _gitcrud(request)
    doc = _overlay(gc, app)
    found = _routing_status(request, app, doc, source_registry)
    if not found.drift:
        return WriteResult(changed=False)
    routing.apply(app.descriptor, doc, found.compiled)
    protected = _enforce(request, user, app.overlay_name, doc)
    return commit_overlay(
        request,
        user,
        app,
        doc,
        summary="sync routing",
        protected=protected,
        if_match=if_match,
    )


# --- dry run ---


def _require_dry_run(request: Request, user: Any) -> None:
    """Gate a dry run on its own grant, not on reading a config value.

    It executes caller-supplied code, which is a strictly higher privilege than
    anything else on this router.
    """
    action = scopes_dict["dryrun_execute"]
    if not authorize(user, action, role_config=request.app.state.role_config).allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": f"requires {action}"})


def _require_sampler_read(request: Request, user: Any) -> None:
    """Gate a dry run on the sampler grant as well as its own.

    A dry run returns real events, so it must not become a way to read rows the
    caller could not have sampled directly.
    """
    action = scopes_dict["sampler_read"]
    if not authorize(user, action, role_config=request.app.state.role_config).allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": f"requires {action}"})


async def _sample_events(
    request: Request, user: Any, source: str, limit: int, ch: Any, source_registry: Any
) -> list[str]:
    """Pull raw event strings from the source this instance is bound to.

    The sample is held to the caller's orgs exactly as ``POST /sample`` holds it.
    """
    sampler = getattr(request.app.state, "sampler", None)
    if sampler is None:
        raise HTTPException(
            503, detail={"code": "not_configured", "message": "Sampler not initialised"}
        )
    # logreducer is not a dependency, so the gated default mode cannot run in a default install.
    req = SampleRequest(mode=SampleMode.RECENT, source=source, limit=limit)
    org_ids = sample_org_scope(request, user)
    try:
        await run_blocking(functools.partial(sampler.resolve_or_raise, req, source_registry))
    except SamplerError as exc:
        raise HTTPException(
            400, detail={"code": "bad_sample_request", "message": str(exc)}
        ) from exc
    await check_sample_scope(sampler, req, ch, source_registry, org_ids)
    try:
        result: dict[str, Any] = await sampler.run(req, ch, source_registry, org_ids=org_ids)
    except SamplerError as exc:
        raise HTTPException(
            400, detail={"code": "bad_sample_request", "message": str(exc)}
        ) from exc
    # Sampler.run returns SampleResult.model_dump(), a dict -- not an object.
    lines = result.get("lines") or []
    return [str(line) for line in lines]


@router.post("/{service}/{instance}/files/{set_name}/dry-run", response_model=DryRunResponse)
async def dry_run_app_file(
    service: str,
    instance: str,
    set_name: str,
    body: DryRunRequest,
    user: CurrentUser,
    request: Request,
    ch: ClickHouseClient,
    source_registry: SourceReg,
) -> DryRunResponse:
    """Run an authored file over real events from the source, and report each one.

    Nothing is written: no topic, no table, no commit. ``content`` runs unsaved
    content, which is what makes this useful in an editor; omitted, the file
    already in the overlay runs instead.
    """
    app = _resolve(service, instance)
    _require_dry_run(request, user)
    _require_sampler_read(request, user)
    fs = _file_set(service, set_name)

    content = body.content
    if content is None:
        doc = await run_blocking(functools.partial(_overlay, _gitcrud(request), app))
        try:
            content = files.read_file(doc, fs, body.name).content
        except FileNotInSetError:
            raise HTTPException(
                404, detail={"code": "no_such_file", "message": f"{body.name} is not in {set_name}"}
            ) from None

    source = body.source or app.instance
    events = await _sample_events(request, user, source, body.limit, ch, source_registry)
    result = await run_blocking(
        functools.partial(
            dryrun.run_language,
            fs.language,
            content,
            events,
            enabled=request.app.state.settings.transform_validation.dry_run,
        )
    )
    audit_resource_change(user.user_id, "dryrun", f"{service}/{instance}/{body.name}", "executed")
    return DryRunResponse(
        status=str(result.status),
        backend=result.backend,
        message=result.message,
        source=source,
        sampled=len(events),
        succeeded=result.succeeded,
        failed=result.failed,
        dropped=result.dropped,
        truncated=result.truncated,
        events=[
            DryRunEventModel(
                index=e.index,
                before=e.before,
                after=e.after,
                error=e.error,
                dropped=e.dropped,
                changed=e.changed,
            )
            for e in result.events
        ],
    )


# --- operational surface ---


def _reader(request: Request, client: Any) -> OperationalReader:
    settings = request.app.state.settings
    return OperationalReader(client, settings.clickhouse.effective_data_database)


def metrics_unavailable(exc: MetricsUnavailableError) -> HTTPException:
    """The 503 for an otel read ClickHouse refused, its text logged rather than sent."""
    return backend_failure(
        503,
        "metrics_unavailable",
        "the telemetry tables could not be read; the engine log has ClickHouse's reason",
        exc,
        event="otel query failed",
    )


@router.get("/{service}/{instance}/status")
def get_status(
    service: str, instance: str, user: CurrentUser, request: Request, client: ClickHouseClient
) -> StatusResponse:
    """Whether the instance is reporting telemetry, and since when."""
    app = _resolve(service, instance)
    _require_metrics_read(request, user, service)
    try:
        status = _reader(request, client).status(app.telemetry_name)
    except MetricsUnavailableError as exc:
        raise metrics_unavailable(exc) from exc
    return StatusResponse(
        telemetry_name=status.telemetry_name,
        reporting=status.reporting,
        last_seen_epoch=status.last_seen_epoch,
        started_epoch=status.started_epoch,
        uptime_seconds=status.uptime_seconds,
    )


@router.get("/{service}/{instance}/metrics")
def get_metrics(
    service: str, instance: str, user: CurrentUser, request: Request, client: ClickHouseClient
) -> MetricsResponse:
    """Throughput, CPU, memory and saturation for the instance."""
    app = _resolve(service, instance)
    _require_metrics_read(request, user, service)
    try:
        found = _reader(request, client).metrics(app.telemetry_name)
    except MetricsUnavailableError as exc:
        raise metrics_unavailable(exc) from exc
    return MetricsResponse(
        telemetry_name=found.telemetry_name,
        window_seconds=found.window_seconds,
        gauges=found.gauges,
        rates=found.rates,
    )


@router.get("/{service}/{instance}/metrics/series")
def get_resource_series(
    service: str,
    instance: str,
    user: CurrentUser,
    request: Request,
    client: ClickHouseClient,
    window_seconds: int = 3600,
    bucket_seconds: int = 60,
) -> ResourceSeriesResponse:
    """CPU and memory over time as min, max, average and p95 per bucket.

    Each bucket aggregates across every pod reporting for this instance. A
    per-config app deploys each config under its own service name, so one instance
    is already one config.
    """
    app = _resolve(service, instance)
    _require_metrics_read(request, user, service)
    window = min(max(window_seconds, 60), 7 * 24 * 3600)
    bucket = min(max(bucket_seconds, 10), window)
    try:
        buckets = _reader(request, client).resource_series(
            app.telemetry_name, window_seconds=window, bucket_seconds=bucket
        )
    except MetricsUnavailableError as exc:
        raise metrics_unavailable(exc) from exc
    return ResourceSeriesResponse(
        telemetry_name=app.telemetry_name,
        window_seconds=window,
        bucket_seconds=bucket,
        buckets=[ResourceBucketModel(**asdict(b)) for b in buckets],
    )
