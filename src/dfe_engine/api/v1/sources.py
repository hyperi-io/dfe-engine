"""Sources router -- CRUD, pagination, search, sort, bulk operations.

GET    /api/v1/sources                  -> Paginated source list
POST   /api/v1/sources                  -> Create source
GET    /api/v1/sources/catalogue        -> The sources DFE ships ready to create
POST   /api/v1/sources/from-catalogue   -> Create sources from catalogue entries
GET    /api/v1/sources/{name}           -> Get source details
GET    /api/v1/sources/{name}/flow      -> The stages its records travel, resolved
GET    /api/v1/sources/{name}/export    -> One version as a portable bundle
POST   /api/v1/sources/import           -> Apply a bundle from another deployment
GET    /api/v1/sources/{name}/versions/{version}  -> Get one version snapshot
GET    /api/v1/sources/{name}/columns   -> Composed schema columns for a version
POST   /api/v1/sources/{name}/build     -> Build DDL from a version snapshot
POST   /api/v1/sources/{name}/plan      -> Dry-run deploy plan (not persisted)
POST   /api/v1/sources/{name}/deploy    -> Deploy version to ClickHouse
PUT    /api/v1/sources/{name}           -> Update source
PATCH  /api/v1/sources/{name}           -> Enable or disable source
DELETE /api/v1/sources/{name}           -> Delete source
POST   /api/v1/sources/bulk             -> Bulk enable/disable/dormant/delete/deploy
POST   /api/v1/sources/seed             -> Seed built-in defaults
POST   /api/v1/sources/reconcile-apps   -> Bring the derived app state into step with the sources

Every write that changes what the apps must do -- a deploy, a state change, a
delete, an edit -- ends by reconciling the deploy repo: the receiver and loader
routing blocks are recompiled, and a fetcher-based source gains or loses its
fetcher instance. The reconcile never fails the source write; its outcome is
reported, and ``reconcile-apps`` retries it.

The reconcile also deletes the ``_load`` topic a dropped transform left, once its
transform instance was already gone before the pass, the transform's consumer
group has no live member, and nothing on the topic is unread.

A deploy also points HyperDX at the table it just made, and a delete takes that
source away again. Like the reconcile, neither ever fails the source write.
"""

import asyncio
import functools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple, NoReturn

from dfe_schemas.clickhouse import PlatformProbe, ReplicationNotConfiguredError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from scalo.api import FieldError as ProblemFieldError
from scalo.api import ProblemError
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.deps import (
    ClickHouseClient,
    CurrentUser,
    SchemaReg,
    Settings,
    SourceReg,
    get_field_map_registry_optional,
    get_interactive_clickhouse_client,
    refuse_unknown_orgs,
    request_tenancy,
    require_action,
    ttl_settings,
)
from dfe_engine.api.errors import (
    TOPICS_NOT_CREATED,
    CoreResourceConflictErrorResponse,
    ErrorResponse,
    InvalidRuleErrorResponse,
    RuleError,
    RuleErrorSpan,
    SourceCreateConflictResponse,
    SourceWriteConflictResponse,
    TopicsNotCreatedErrorResponse,
    backend_failure,
    hide_backend_text,
    raise_exchange_http,
)
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.api.v1.apps import commit_overlay, metrics_unavailable, remove_overlay
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import (
    LOADER_COMPILER,
    TRANSFORM_COMPILER,
    MetricsUnavailableError,
    OperationalReader,
    appconfig,
    derived,
    instances,
)
from dfe_engine.appmgmt import catalogue as app_catalogue
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.exchange.models import SourceBundle, SourceImportResult
from dfe_engine.exchange.schemas import ExchangeError
from dfe_engine.exchange.sources import apply_source_bundle, build_source_bundle
from dfe_engine.git_identity import git_author
from dfe_engine.manifest import ManifestError
from dfe_engine.orgs.ingest_tokens import BearerSourceUnreadableError, resync_bearer_tokens
from dfe_engine.schema.applier import SchemaApplyError, live_tables
from dfe_engine.schema.derived_registry import derived_reference_root
from dfe_engine.schema.lock import SchemaLockBusyError, SchemaLockError, ddl_lease
from dfe_engine.settings import get_settings
from dfe_engine.source import catalogue as source_catalogue_module
from dfe_engine.source.deployment import (
    DeployStatements,
    SchemaDeployResult,
    SettingsChange,
    SourceBuildArtifact,
    SourceDeploymentStore,
    SourcePlanArtifact,
    TtlChange,
    ensure_build_artifact,
    execute_ddl,
    plan_deploy_statements,
    plan_from_build,
    previous_deployed_version_ids,
    settings_change_for_table,
    ttl_change_for_table,
)
from dfe_engine.source.flow import FlowError, SourceFlow, resolve_flow
from dfe_engine.source.models import (
    DEFAULT_LANDING_LABEL,
    PaginatedSourceSummaryResponse,
    Source,
    SourceState,
    SourceSummaryObject,
    SourceVersion,
    SourceVersionGetResponse,
    SourceWriteRequest,
    engine_registry,
)
from dfe_engine.source.registry import (
    SourceAliasConflictError,
    SourceCoreResourceError,
    SourceMatchConflictError,
    SourceNotFoundError,
    SourceRuleError,
    SourceValidationError,
)
from dfe_engine.transport import SourceTransport

router = APIRouter(prefix="/sources", tags=["Sources"], dependencies=[WRITE_TURN])

_CH_UNREADABLE = "ClickHouse could not be read; the engine log has the reason"


def _ch_unreadable(exc: Exception, source: str) -> HTTPException:
    """The 503 for a deploy or plan that could not read ClickHouse's state."""
    return backend_failure(
        503,
        "clickhouse_unavailable",
        _CH_UNREADABLE,
        exc,
        event="source deploy: ClickHouse unreadable",
        source=source,
    )


def _require_replication(probe: PlatformProbe, source: str) -> None:
    """Refuse a deploy to a server the schema phase would refuse, with dfe-schemas' reason.

    Raises:
        HTTPException: 503 ``clickhouse_not_replicated`` carrying the reason, which
            dfe-schemas composes and which names no host, or 503
            ``clickhouse_unavailable`` when the server could not be asked.
    """
    try:
        probe.require_replication()
    except ReplicationNotConfiguredError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "clickhouse_not_replicated", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise _ch_unreadable(exc, source) from exc


_INVALID_RULE: dict[int | str, dict[str, Any]] = {
    422: {
        "model": InvalidRuleErrorResponse | ErrorResponse,
        "description": (
            "Refused, nothing written. The body failed validation (code validation_error), or "
            "the receiver would refuse the source's rule (code invalid_rule): context.rule_errors "
            "names each field at fault and, for when, the span the check stopped at"
        ),
    },
}


def invalid_rule_problem(exc: SourceRuleError) -> ProblemError:
    """The 422 for a source whose rule the receiver would refuse, each problem with its span."""
    return ProblemError(
        422,
        str(exc),
        code="invalid_rule",
        errors=[
            ProblemFieldError(
                field=problem.field, location="body", detail=problem.detail, code="invalid_rule"
            )
            for problem in exc.problems
        ],
        source=exc.source,
        rule_errors=[
            RuleError(
                field=problem.field,
                message=problem.detail,
                span=None
                if problem.span is None
                else RuleErrorSpan(start=problem.span[0], end=problem.span[1]),
            ).model_dump(mode="json")
            for problem in exc.problems
        ],
    )


def _raise_save_validation_http(exc: SourceValidationError) -> NoReturn:
    """Map registry validation errors to HTTP responses (never 500)."""
    # 409, matching the core-resource guard's answer for roles, schemas and field maps.
    if isinstance(exc, SourceCoreResourceError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": str(exc),
                "source": exc.source,
            },
        ) from exc
    if isinstance(exc, SourceMatchConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "match_conflict",
                "message": str(exc),
                "source": exc.source,
                "conflicting_source": exc.conflicting_source,
                "field": exc.field,
                "value": exc.value,
            },
        ) from exc
    if isinstance(exc, SourceAliasConflictError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "alias_conflict",
                "message": str(exc),
                "source": exc.source,
                "conflicting_source": exc.conflicting_source,
                "profile": exc.profile,
                "alias": exc.alias,
            },
        ) from exc
    if isinstance(exc, SourceRuleError):
        raise invalid_rule_problem(exc) from exc
    raise HTTPException(
        status_code=422,
        detail={
            "code": "validation_error",
            "message": str(exc),
        },
    ) from exc


def _failure_code(exc: Exception) -> str:
    """The code a bulk entry reports, so one refusal reads the same on either route.

    Ordered narrowest first: the two conflict errors both subclass
    ``SourceValidationError``.
    """
    if isinstance(exc, SourceCoreResourceError):
        return "conflict"
    if isinstance(exc, SourceMatchConflictError):
        return "match_conflict"
    if isinstance(exc, SourceAliasConflictError):
        return "alias_conflict"
    if isinstance(exc, SourceRuleError):
        return "invalid_rule"
    if isinstance(exc, SourceNotFoundError):
        return "not_found"
    if isinstance(exc, SourceValidationError):
        return "validation_error"
    return "internal_error"


# -- Response models --


class SourceResponse(BaseModel):
    """Legacy compact metadata after create/update (prefer full ``Source`` on write)."""

    source: str
    message: str = "ok"
    current: str = Field(..., description="Working version id after the operation")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to runtime (null until first deploy)",
    )
    versions: list[str] = Field(..., description="All version ids on the source")
    apps_synced: list[str] = Field(
        default_factory=list,
        description="Deploy-repo writes made so the apps follow this change (service/instance: action)",
    )
    apps_sync_error: str | None = Field(
        default=None,
        description="Why the apps could not be brought into step; reconcile-apps retries it",
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description=(
            "One command per app whose running process cannot take this change where "
            "it stands. Empty where every write was hot, or where a GitOps controller "
            "rolls the pod itself."
        ),
    )


def _apps_synced_field() -> Any:
    return Field(
        default_factory=list,
        description="Deploy-repo writes made so the apps follow this change (service/instance: action)",
    )


def _apps_sync_error_field() -> Any:
    return Field(
        default=None,
        description="Why the apps could not be brought into step; reconcile-apps retries it",
    )


def _apply_hints_field() -> Any:
    return Field(
        default_factory=list,
        description=(
            "One command per app the change leaves to apply where this deployment renders "
            "the app config itself, including taking away the container of an instance a "
            "source no longer has. Empty where a GitOps controller applies it."
        ),
    )


class SourceDeleteResponse(BaseModel):
    """A deleted source, and what the apps now need to follow the delete."""

    source: str
    message: str = "deleted"
    apps_synced: list[str] = _apps_synced_field()
    apps_sync_error: str | None = _apps_sync_error_field()
    restart_required: list[str] = _apply_hints_field()


class SourceImportResponse(SourceImportResult):
    """A bundle applied, plus what the apps now need to follow it."""

    apps_synced: list[str] = Field(
        default_factory=list,
        description="Deploy-repo writes made so the apps follow the imported source",
    )
    apps_sync_error: str | None = Field(
        default=None,
        description="Why the apps could not be brought into step; reconcile-apps retries it",
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description="One command per app whose running process cannot take this change in place",
    )


class AppsReconcileResponse(BaseModel):
    """What the reconcile wrote into the deploy repo."""

    changes: list[str] = Field(
        default_factory=list, description="Overlay writes made (service/instance: action)"
    )
    restart_required: list[str] = Field(
        default_factory=list,
        description=(
            "One command per app whose running process cannot take this change where it stands."
        ),
    )
    topics_removed: list[str] = Field(
        default_factory=list,
        description=(
            "Stranded _load topics deleted: no transform instance writing one was in "
            "the deploy repo before this pass, the transform's consumer group had no "
            "live member, and nothing on it was unread"
        ),
    )
    topics_pending_removal: list[str] = Field(
        default_factory=list,
        description=(
            "Stranded _load topics kept, because a transform instance writing one was "
            "in the deploy repo before this pass, the transform's consumer group has a "
            "live member or cannot be named, or a consumer group has records left on it"
        ),
    )


@dataclass(frozen=True, slots=True)
class AppsSync:
    """What bringing the apps into step with the sources did, and what it now needs."""

    changes: list[str]
    error: str | None = None
    restart_required: list[str] = field(default_factory=list)
    topics_removed: list[str] = field(default_factory=list)
    topics_pending_removal: list[str] = field(default_factory=list)


def _source_response(
    source: Source,
    *,
    message: str,
    apps: AppsSync | None = None,
) -> SourceResponse:
    synced = apps or AppsSync(changes=[])
    return SourceResponse(
        source=source.source,
        message=message,
        current=source.current,
        deployed_version=source.deployed_version,
        versions=sorted(source.versions.keys()),
        apps_synced=synced.changes,
        apps_sync_error=synced.error,
        restart_required=synced.restart_required,
    )


def _one_per_app(hints: list[str]) -> list[str]:
    """The restart hints in the order first reported, with repeats dropped.

    Every write renders every app, so two overlays written in one reconcile can
    each report the same app; the caller runs one command per app.
    """
    return list(dict.fromkeys(hints))


def _refuse_unknown_fetched_org(request: Request, body: SourceWriteRequest) -> None:
    """Refuse a fetched source naming an org the registry does not hold.

    Raises:
        HTTPException: 422 ``unknown_org``.
    """
    if body.fetcher is not None and body.fetcher.org is not None:
        refuse_unknown_orgs(request, [body.fetcher.org])


def reconcile_derived_apps(request: Request, user: Any, registry: Any) -> AppsSync:
    """Apply what the sources and the orgs imply about the deployed apps. Never raises.

    Returns the writes made, why the reconcile could not complete when it did
    not, and the restart each app needs where this deployment renders its config
    files itself. A deployment without a deploy repo has nothing to reconcile.

    The hints are collected from the writes as well as from the render that
    follows them, because the overlay write renders the app's config on its way
    through: by the time the reconcile renders again the file is already current,
    so an app whose config only that write changed would be reported as needing
    nothing and a Compose stack would leave it on the source it started with.

    A stranded ``_load`` topic is judged on the transform instances the deploy
    repo carried BEFORE this pass's writes, so the pass that removes an instance
    never deletes the topic its still-running process may write.

    The receiver's bearer secrets are rewritten first, so the auth this pass compiles
    reads tokens that are there: a mirror Secret recreated empty is refilled here.
    """
    state = request.app.state
    if getattr(state, "org_registry", None) is not None and hasattr(state, "dfe_secrets"):
        resync_bearer_tokens(
            state.org_registry, state.dfe_secrets, getattr(state, "bearer_mirror", None)
        )
    gc = getattr(state, "gitcrud", None)
    if gc is None:
        return AppsSync(changes=[])
    settings = request.app.state.settings
    done: list[str] = []
    hints: list[str] = []
    try:
        changes = derived.plan(gc, registry, settings, tenancy=request_tenancy(request))
        transforms = transform_instances(gc)
        for change in changes:
            if change.action == "forget":
                derived.drop_parked(gc, change.app, user.user_id)
                done.append(change.describe())
                continue
            if change.action == "remove":
                derived.keep_parked(gc, change, user.user_id)
                written = remove_overlay(request, user, change.app)
            else:
                # No _enforce: compiled from the sources, and resolve_mode PRs it in production.
                written = commit_overlay(
                    request, user, change.app, change.doc or {}, summary=change.summary
                )
                # A restore held for review keeps its parked copy until the overlay lands.
                if change.unpark and not written.review_required:
                    derived.drop_parked(gc, change.app, user.user_id)
            # Each write renders the app config itself, so its hint is the only
            # report of that change: the render below finds the file current.
            hints += written.restart_required
            done.append(change.describe())
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else exc.detail.get("message", "")
        logger.warning(f"apps not reconciled with the sources: {detail}")
        return AppsSync(changes=done, error=str(detail), restart_required=_one_per_app(hints))
    except BearerSourceUnreadableError as exc:
        # The engine's own text, naming the setting to change, so it reaches the caller.
        logger.warning(f"apps not reconciled with the sources: {exc}")
        return AppsSync(changes=done, error=str(exc), restart_required=_one_per_app(hints))
    except Exception as exc:
        # Anything but an HTTPException can be the deploy repo's own text.
        error = hide_backend_text(
            "the deploy repo could not be brought into step; the engine log has the reason",
            exc,
            event="apps not reconciled with the sources",
        )
        return AppsSync(changes=done, error=error, restart_required=_one_per_app(hints))
    # Re-rendered even when the reconcile wrote nothing: a source deploy changes
    # the loader's table map through the same overlay the plan found in step.
    hints += appconfig.render_and_report(gc, settings)
    removed, pending = sweep_stranded_topics(registry, settings, transforms, actor=user.user_id)
    return AppsSync(
        changes=done,
        restart_required=_one_per_app(hints),
        topics_removed=removed,
        topics_pending_removal=pending,
    )


async def _sync_hyperdx_source(
    request: Request, source: Source, database: str, columns: list[str]
) -> tuple[int | None, str | None]:
    """Point HyperDX at the table this deploy just made. Never raises.

    Returns how many HyperDX teams now carry the source and, when nothing was
    written, why. A deployment without HyperDX has nothing to point at.
    """
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        return None, None

    from dfe_engine.hyperdx.sources import ensure_source

    try:
        teams = await ensure_source(
            client,
            name=source.source,
            database=database,
            table=source.table_name,
            columns=columns,
        )
    except Exception as exc:
        return None, hide_backend_text(
            "Search did not accept the source; the engine log has the reason",
            exc,
            event="HyperDX not pointed at the source",
            source=source.source,
        )
    if teams is None:
        return None, "Search did not accept the source; see the engine log"
    return len(teams), None


async def _rebuild_all_sources(*, registry: Any, settings: Any, sources: list[str]) -> None:
    """Rebuild the all_sources Merge table off the event loop after a write to *sources*. Never raises."""
    from dfe_engine.clickhouse.all_sources import try_rebuild_all_sources

    await run_blocking(
        functools.partial(
            try_rebuild_all_sources,
            changed=sources,
            load_sources=registry.get_all_sources,
            settings=settings,
        )
    )


async def _register_hyperdx_all_sources(*, database: str, request: Request) -> None:
    """Point HyperDX at the all_sources Merge table on every team. Never raises."""
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        return

    from dfe_engine.http_errors import describe_http_error
    from dfe_engine.hyperdx.all_sources import register_all_sources

    try:
        await register_all_sources(client=client, database=database)
    except Exception as exc:
        logger.warning("HyperDX all_sources source not registered", **describe_http_error(exc=exc))


async def _remove_hyperdx_source(request: Request, name: str) -> None:
    """Drop the HyperDX source for a DFE source that is going away. Never raises."""
    client = getattr(request.app.state, "hyperdx_client", None)
    if client is None:
        return

    from dfe_engine.hyperdx.sources import remove_source

    try:
        await remove_source(client, name=name)
    except Exception as exc:
        logger.warning(f"HyperDX source not removed for '{name}': {exc}")


def _resolve_source(name: str, registry: Any) -> Source:
    """One source, or 404."""
    try:
        return registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source {name!r} not found"},
        ) from None


def _landing_table(source: Source) -> tuple[str, bool]:
    """The table this source's records land in, and whether that is the shared default.

    The loader takes the table from the ``_source`` label the producer stamped,
    and the ``source_to_table`` map the compile emits is the identity - so the
    table IS the landing label. A fetcher pushing to the main topic therefore
    lands in the platform's landing table rather than in one of its own name.
    """
    label = source.landing_label()
    return label, label == DEFAULT_LANDING_LABEL


def _deployed_loaders(request: Request) -> list[str]:
    """The OTel service names of the deployed instances that count records per table.

    Read from the manifest's routing compiler rather than from an app name, so an
    org that renames or replaces the loading stage is measured just the same.
    """
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        return []
    return [i.telemetry_name for i in instances.instances_routed_by(gc, LOADER_COMPILER)]


class SourceEnabledPatchRequest(BaseModel):
    """Partial update for source lifecycle state only.

    Send ``state`` for the tri-state (active | dormant | disabled), or the
    compat boolean ``enabled`` (True -> active, False -> disabled). ``state``
    wins when both are sent.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(
        default=None,
        description="Compat boolean lifecycle (True -> active, False -> disabled)",
    )
    state: SourceState | None = Field(
        default=None,
        description="Tri-state lifecycle (active | dormant | disabled); wins over enabled",
    )

    @model_validator(mode="after")
    def _require_one(self) -> SourceEnabledPatchRequest:
        if self.state is None and self.enabled is None:
            raise ValueError("Send 'state' or 'enabled'")
        return self

    def target_state(self) -> SourceState:
        """The tri-state this patch requests."""
        if self.state is not None:
            return self.state
        return "active" if self.enabled else "disabled"


class BulkActionItem(BaseModel):
    """One source in a bulk request."""

    source: str = Field(description="Source name (_source label)")
    version: str | None = Field(
        default=None,
        description=(
            "Deploy only: version id to deploy. Omitted uses the working current. "
            "Ignored for enable/disable/dormant/delete."
        ),
    )


class BulkActionRequest(BaseModel):
    """Bulk operation on multiple sources."""

    action: str = Field(
        description="Action: enable, disable, dormant, delete, deploy",
    )
    sources: list[BulkActionItem] = Field(
        min_length=1,
        description="Sources to act on. Plain name strings are accepted.",
    )
    dry_run: bool = Field(
        default=False,
        description="Deploy only: plan/validate DDL without applying it. Ignored otherwise.",
    )

    @field_validator("sources", mode="before")
    @classmethod
    def _coerce_source_names(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [{"source": item} if isinstance(item, str) else item for item in value]


class BulkDeployTask(BaseModel):
    """One accepted deploy submitted as a background task."""

    source: str
    version: str | None = Field(
        default=None,
        description="Version requested; null means the working current at task start",
    )
    task_id: str = Field(description="Task id for polling via GET /tasks/{task_id}")
    kind: str = Field(default="source:deploy", description="Task kind")


class BulkActionResponse(BaseModel):
    """Result of a bulk operation."""

    action: str
    succeeded: list[str] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "One entry per source left untouched: its name, the same code the "
            "single-source route answers with, and the message"
        ),
    )
    deployments: list[BulkDeployTask] = Field(
        default_factory=list,
        description=(
            "Deploy only: one ``source:deploy`` task per accepted source. "
            "Poll via GET /tasks/{task_id}."
        ),
    )
    apps_synced: list[str] = _apps_synced_field()
    apps_sync_error: str | None = _apps_sync_error_field()
    restart_required: list[str] = _apply_hints_field()


class SeedResponse(BaseModel):
    """Result of seeding built-in sources."""

    seeded: int = Field(description="Number of sources seeded")


class TableEngineObject(BaseModel):
    """One table engine a source may select, as the console offers it."""

    name: str = Field(description="MergeTree-family variant, e.g. ReplacingMergeTree")
    description: str = Field(description="What the engine does with rows")
    arguments: Literal["none", "optional", "required"] = Field(
        description="Whether the variant takes arguments inside its parentheses"
    )
    argument_hint: str = Field(
        description="What goes inside the parentheses; empty when arguments is none"
    )


class CatalogueEntryObject(BaseModel):
    """One source DFE ships ready to create, as the console lists it."""

    name: str = Field(description="The source name the entry creates, and its catalogue key")
    display_name: str | None = Field(default=None, description="Human-readable display name")
    description: str | None = Field(default=None, description="What the source carries")
    package: str = Field(description="Vendor integration package it came from")
    data_stream: str = Field(description="Data stream within that package")
    dataset: str = Field(description="package.data_stream - what a Beats event stamps")
    intakes: list[str] = Field(description="Ways this source's data can reach the platform")
    default_intake: source_catalogue_module.CatalogueIntake = Field(
        description="The intake a create takes when it names none: the first of fetcher, "
        "beats and receiver the entry lists"
    )
    framing: str | None = Field(
        default=None,
        description="Receiver intakes only: whether the pipeline wants the syslog line or the body",
    )
    beats: dict[str, str] = Field(
        default_factory=dict,
        description="Beats module and fileset carrying the same source, when one does",
    )
    has_schema: bool = Field(
        description="Whether the entry binds a table schema. A fetcher intake without one is "
        "refused on create (422), because a fetcher on its own topic needs one"
    )
    engine: str | None = Field(
        default=None, description="The transform engine the source runs, e.g. elastic"
    )
    variant: str | None = Field(
        default=None, description="The variant a create runs when it names none"
    )
    variants: list[str] = Field(
        default_factory=list, description="Every variant the entry compiles to, the default first"
    )
    availability: source_catalogue_module.Availability = Field(
        description="Whether the deployed transform ships the default variant, read off its "
        "mounted contract: available, missing_variant (a create is refused with 409), or "
        "unknown (no contract is mounted or it is unreadable, and a create is allowed)"
    )
    availability_reason: str = Field(description="Why availability says what it does")


class CatalogueSourceItem(BaseModel):
    """One catalogue entry to compile into a source."""

    model_config = ConfigDict(extra="forbid")

    entry: str = Field(min_length=1, description="Catalogue entry name")
    intake: source_catalogue_module.CatalogueIntake | None = Field(
        default=None,
        description="How this source's data arrives, the entry's default intake when omitted",
    )
    name: str | None = Field(
        default=None,
        description="Source name, the entry's own name when omitted",
    )
    variant: str | None = Field(
        default=None,
        description="The full name of one of the entry's variants, the first when omitted",
    )
    transport: SourceTransport | None = Field(
        default=None,
        description="bus or direct; omitted takes the deployment default",
    )
    archive: bool = Field(
        default=False,
        description="Keep the raw record as it arrived; needs the bus transport",
    )


class CatalogueSourcesRequest(BaseModel):
    """The catalogue entries to turn into sources, in one request."""

    model_config = ConfigDict(extra="forbid")

    entries: list[CatalogueSourceItem] = Field(
        min_length=1,
        max_length=50,
        description="Entries to create, in the order given. At most 50.",
    )


class CatalogueCreatedSource(BaseModel):
    """One source stored from a catalogue entry."""

    entry: str = Field(description="Catalogue entry this source was compiled from")
    source: str
    current: str = Field(description="Working version id after the create")
    deployed_version: str | None = Field(
        default=None,
        description="Version deployed to runtime (null until first deploy)",
    )
    versions: list[str] = Field(description="All version ids on the source")


class CatalogueSourceFailure(BaseModel):
    """One catalogue entry that was not stored, and why."""

    entry: str
    source: str = Field(default="", description="Source name, once the entry compiled that far")
    code: str
    message: str


class CatalogueSourcesResponse(BaseModel):
    """What creating the requested catalogue entries did.

    ``created`` is what was stored. ``failed`` is what was left unstored. A later
    entry failing does not remove an earlier one. The apps are reconciled once,
    for everything that was stored.
    """

    created: list[CatalogueCreatedSource]
    failed: list[CatalogueSourceFailure]
    apps_synced: list[str] = _apps_synced_field()
    apps_sync_error: str | None = _apps_sync_error_field()
    restart_required: list[str] = _apply_hints_field()


class SchemaColumn(BaseModel):
    """A column in a composed source schema (API response)."""

    name: str
    type: str
    use_case: str = ""
    attribute: list[str] | None = None
    description: str = ""


class DDLResult(BaseModel):
    """Generated DDL output."""

    source_name: str
    create_table: str = Field(description="CREATE TABLE DDL")
    views: dict[str, str] = Field(default_factory=dict, description="View name -> DDL")


class SchemaBuildResult(BaseModel):
    """Result of building a schema from a source."""

    source_name: str
    version: str = ""
    columns: list[SchemaColumn]
    ddl: DDLResult | None = None
    validation_errors: list[str] = Field(default_factory=list)
    built_at: str | None = Field(
        default=None,
        description="When the build was persisted (source-builds); omitted on live-only builds",
    )
    column_count: int | None = Field(
        default=None,
        description="Column count at build time when columns are not included in the payload",
    )


class SourcePlanResponse(BaseModel):
    """Dry-run ClickHouse deploy plan for a source version."""

    source_name: str
    version: str
    planned_at: str
    table_exists: bool = False
    validation_errors: list[str] = Field(default_factory=list)
    statements: list[str] = Field(default_factory=list)
    ddl: DDLResult | None = None
    ready: bool = False
    ready_reason: str | None = Field(
        default=None,
        description="Why the plan is or is not ready to deploy",
    )
    ttl_change: TtlChange | None = Field(
        default=None,
        description=(
            "The TTL move among the statements, on a table that already exists; "
            "null when the TTL does not move"
        ),
    )


class SourceVersionDetail(SourceVersion):
    """Source version snapshot plus persisted build/deploy payloads."""

    source_build: SchemaBuildResult | None = Field(
        default=None,
        description="Last schema build for this version (source-builds)",
    )
    source_deployment: SchemaDeployResult | None = Field(
        default=None,
        description="Last deploy run for this version (source-deploys)",
    )


class SourceDetailResponse(Source):
    """Full source definition with per-version build/plan/deploy status."""

    versions: dict[str, SourceVersionDetail] = Field(
        default_factory=dict,
        description="Version id -> configuration snapshot and pipeline artifacts",
    )


class FlowTransformModel(BaseModel):
    """The transform stage, when the source has one."""

    app: str = Field(description="Catalogued app running it, e.g. dfe-transform-vrl")
    instance: str = Field(description="Its deployed name, e.g. dfe-transform-vrl-auth")
    variant: str | None = Field(
        default=None,
        description="The compiled-in program it runs, where the app offers a catalogue of them",
    )
    endpoint: str | None = Field(
        default=None, description="Direct: the address records reach it on. Null on the bus."
    )
    topics: list[str] | None = Field(
        default=None, description="Bus: the landing and transformed topics. Null on direct."
    )


class FlowOutputsModel(BaseModel):
    """Where the records end up."""

    loader: str = Field(
        description="The topic the loader consumes, or the endpoint it is pushed to"
    )
    archive: bool = Field(
        description="Whether the archiver also keeps the raw record off the landing topic"
    )


class SourceFlowResponse(BaseModel):
    """One source's whole path, as the resolver reports it.

    The wire shape of ``dfe_engine.source.flow.SourceFlow``, and the only copy of
    it: everything here is built by ``_flow_response`` from that dataclass, so a
    stage the resolver gains is a field added in one place.
    """

    source: str
    transport: str = Field(
        description="bus: a broker holds records between stages; direct: no store"
    )
    carrier: str = Field(
        description="What carries it here - the bus provider, or the direct protocol"
    )
    origin: str | None = Field(
        default=None,
        description="receiver, fetcher, or null when nothing selects the records",
    )
    input: str = Field(
        description="The receiver match that selects the records, or the fetcher instance polling them"
    )
    transform: FlowTransformModel | None = None
    outputs: FlowOutputsModel
    table: str


def _flow_response(flow: SourceFlow) -> SourceFlowResponse:
    """The resolver's answer on the wire."""
    return SourceFlowResponse(
        source=flow.source,
        transport=flow.transport,
        carrier=flow.carrier,
        origin=flow.origin,
        input=flow.input,
        transform=(
            FlowTransformModel(
                app=flow.transform.app,
                instance=flow.transform.instance,
                variant=flow.transform.variant,
                endpoint=flow.transform.endpoint,
                topics=list(flow.transform.topics) if flow.transform.topics else None,
            )
            if flow.transform
            else None
        ),
        outputs=FlowOutputsModel(loader=flow.outputs.loader, archive=flow.outputs.archive),
        table=flow.table,
    )


class SourceSignalsResponse(BaseModel):
    """What this source's records are actually doing, beside its definition.

    A reading is null when the window holds nothing to compute it from, and the
    console hides a null rather than showing a zero: an absent series means "not
    measurable here", which is a different statement from "no records".
    """

    source: str
    table: str = Field(description="The ClickHouse table these records land in.")
    landed_in_default: bool = Field(
        description=(
            "True when that table is the platform's default landing table, which "
            "every unmatched record shares. The loader counts records per TABLE, "
            "so the rate below then covers the whole table rather than this source "
            "alone."
        )
    )
    window_seconds: int = Field(description="How far back the readings look.")
    records_per_min: float | None = Field(
        description="Records a minute across the window; null when no series answers."
    )
    last_seen: str | None = Field(
        description=(
            "UTC ISO-8601 timestamp of the last increase in the window; null when "
            "the counter never moved. A flat counter keeps reporting after traffic "
            "stops, so a present series is not itself an arrival."
        )
    )


class SourceVersionGetDetailResponse(SourceVersionGetResponse):
    """Single-version GET with build/plan/deploy status on ``version``."""

    version: SourceVersionDetail = Field(
        ...,
        description="Configuration snapshot for ``selected`` plus pipeline artifacts",
    )


# -- Endpoints --


@router.get(
    "",
    response_model=PaginatedSourceSummaryResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def list_sources(
    user: CurrentUser,
    registry: SourceReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in source name and description"),
    enabled: bool | None = Query(None, description="Filter by enabled status"),
    sort_by: str | None = Query(
        None,
        description="Sort field (source, display_name, enabled, current, deployed_version, updated_at)",
    ),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
):
    """List sources with pagination, search, filtering, and a full object tree.

    The landing source (``main``) is always the first item when it is in the
    result set; remaining sources keep the requested sort.
    """
    raw_sources = registry.list_sources(enabled_only=bool(enabled))

    if enabled is False:
        raw_sources = [s for s in raw_sources if not s.get("enabled", True)]

    raw_sources = apply_search(raw_sources, search, ["source", "display_name", "description"])
    raw_sources = apply_sort(raw_sources, sort_by, sort_order)
    raw_sources = _pin_landing_source_first(raw_sources)

    summaries = [_to_summary(row) for row in raw_sources]
    return PaginatedSourceSummaryResponse.from_summaries(
        summaries, pagination.page, pagination.per_page
    )


_CREATED_APPS_NOT_SYNCED = {
    "model": SourceResponse,
    "description": (
        "The source was stored and the apps were not brought into step with it: "
        "apps_sync_error says why, and POST /sources/reconcile-apps retries it"
    ),
}


@router.post(
    "",
    response_model=SourceResponse,
    status_code=201,
    responses={
        202: _CREATED_APPS_NOT_SYNCED,
        409: {
            "model": SourceCreateConflictResponse,
            "description": (
                "Source name already exists (code conflict), the name is the core "
                "landing the engine owns and reconciles itself (code conflict), "
                "receiver match duplicates another enabled source (code match_conflict), "
                "or an alias is another source's under the same profile (code alias_conflict)"
            ),
        },
        **_INVALID_RULE,
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def create_source(
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
    response: Response,
):
    """Create a new source from a flat source definition (initial version ``1.0.0``).

    ``header`` is optional: when omitted, no common-header profile is stored on the
    version (DDL compose uses meta/derived columns only until a header is set).

    A schema pin naming nothing is accepted here so a source can be written before
    its schema, while ``/sources/import`` refuses one because an exported bundle
    brings every schema it pins.

    201 when the apps follow the new source, 202 when the source is stored and the
    reconcile that brings the apps into step with it did not complete.
    """
    name = body.source

    if not name:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "'source' field is required",
            },
        )

    if registry.source_exists(name):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": f"Source {name!r} already exists",
            },
        )
    _refuse_unknown_fetched_org(request, body)

    try:
        source = registry.create_source_from_write(body, created_by=git_author(user))
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    audit_resource_change(user.user_id, "source", source.source, "created")
    apps = reconcile_derived_apps(request, user, registry)
    if apps.error is not None:
        response.status_code = 202
    return _source_response(source, message="created", apps=apps)


@router.get(
    "/catalogue",
    response_model=PaginatedResponse[CatalogueEntryObject],
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def list_catalogue(
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
    intake: str | None = Query(
        None, description="Only entries that arrive this way (beats, receiver, fetcher)"
    ),
    search: str | None = Query(None, description="Search the entry name, package and data stream"),
) -> PaginatedResponse[CatalogueEntryObject]:
    """List the sources the installed dfe-schemas catalogue ships ready to create.

    Each entry says whether the deployed transform ships its default variant, read off that app's mounted contract, so a console can hold back an entry the create would refuse.
    """
    catalogue = _source_catalogue()
    if intake is not None and intake not in source_catalogue_module.INTAKES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": (
                    f"Unknown intake {intake!r}. Must be: "
                    f"{', '.join(sorted(source_catalogue_module.INTAKES))}"
                ),
            },
        )

    entries = [
        entry for entry in catalogue.entries.values() if intake is None or intake in entry.intakes
    ]
    if search:
        needle = search.lower()
        entries = [
            entry
            for entry in entries
            if needle in entry.name.lower()
            or needle in entry.package.lower()
            or needle in entry.data_stream.lower()
        ]
    # Entries are held in name order, and only the page served is judged against the contract.
    page = PaginatedResponse[str].from_list(
        [entry.name for entry in entries], pagination.page, pagination.per_page
    )
    rows = [_to_catalogue_object(entry=catalogue.entries[name]) for name in page.items]
    return PaginatedResponse[CatalogueEntryObject](
        items=rows, total=page.total, page=page.page, per_page=page.per_page
    )


@router.get(
    "/engines",
    response_model=PaginatedResponse[TableEngineObject],
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def list_table_engines(
    user: CurrentUser,
    pagination: PaginationParams = Depends(),
) -> PaginatedResponse[TableEngineObject]:
    """List the table engines a source may select, from dfe-schemas ``registries/engines.yaml``."""
    rows = [
        TableEngineObject(
            argument_hint=option.argument_hint,
            arguments=option.arguments,
            description=option.description,
            name=option.name,
        )
        for option in engine_registry().options
    ]
    return PaginatedResponse.from_list(rows, pagination.page, pagination.per_page)


_CATALOGUE_CONFLICTS = frozenset(
    {"alias_conflict", "conflict", "match_conflict", "missing_variant"}
)


def _catalogue_failure(
    entry: str, exc: Exception, *, source: str = "", code: str = ""
) -> CatalogueSourceFailure:
    """One entry's refusal, using the same codes the single-source create answers with."""
    if not code:
        if isinstance(exc, source_catalogue_module.SourceCatalogueError):
            code = "catalogue_error"
        elif isinstance(exc, SourceValidationError):
            code = _failure_code(exc)
        else:
            code = "validation_error"
    if isinstance(exc, (SourceCoreResourceError, SourceMatchConflictError)):
        source = source or exc.source
    return CatalogueSourceFailure(entry=entry, source=source, code=code, message=str(exc))


def _catalogue_http_failure(
    entry: str, exc: HTTPException, *, source: str
) -> CatalogueSourceFailure:
    """A refusal raised as HTTP, recorded on the entry instead of aborting the batch."""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("code", "validation_error")
    message = detail.get("message", exc.detail)
    return CatalogueSourceFailure(
        entry=entry,
        source=source,
        code=code if isinstance(code, str) else "validation_error",
        message=message if isinstance(message, str) else str(exc.detail),
    )


def _compile_catalogue_item(
    catalogue: source_catalogue_module.SourceCatalogue,
    item: CatalogueSourceItem,
    *,
    request: Request,
) -> SourceWriteRequest | CatalogueSourceFailure:
    """The write body for one entry, or the refusal that stops that entry only."""
    try:
        write = source_catalogue_module.write_request_for(
            archive=item.archive,
            entry=catalogue.entry(name=item.entry),
            intake=item.intake,
            name=item.name,
            transport=item.transport,
            variant=item.variant,
        )
        _refuse_unknown_fetched_org(request, write)
    except source_catalogue_module.SourceCatalogueError as exc:
        return _catalogue_failure(item.entry, exc)
    except ValueError as exc:
        return _catalogue_failure(item.entry, exc)
    except HTTPException as exc:
        return _catalogue_http_failure(item.entry, exc, source=write.source)
    available = source_catalogue_module.variant_availability(transform=write.transform)
    if available.state == "missing_variant" and write.transform is not None:
        return CatalogueSourceFailure(
            entry=item.entry,
            source=write.source,
            code="missing_variant",
            message=available.reason,
        )
    return write


def _catalogue_status(
    created: list[CatalogueCreatedSource],
    failed: list[CatalogueSourceFailure],
    *,
    apps_error: str | None,
) -> int:
    """201 when every entry was stored, 202 when the apps did not follow, 200 when some were refused.

    Nothing stored is 409 when every refusal is a conflict or a missing variant, and 422 otherwise.
    """
    if created:
        if apps_error:
            return 202
        if failed:
            return 200
        return 201
    if failed and all(item.code in _CATALOGUE_CONFLICTS for item in failed):
        return 409
    return 422


@router.post(
    "/from-catalogue",
    response_model=CatalogueSourcesResponse,
    status_code=201,
    responses={
        200: {
            "model": CatalogueSourcesResponse,
            "description": (
                "Some entries were stored and some were refused. failed names each "
                "refusal; created is what was stored"
            ),
        },
        202: {
            "model": CatalogueSourcesResponse,
            "description": (
                "The entries in created were stored, and the apps were not brought "
                "into step with them: apps_sync_error says why, and POST "
                "/sources/reconcile-apps retries it"
            ),
        },
        409: {
            "model": CatalogueSourcesResponse,
            "description": (
                "Nothing was stored. Every entry was refused because it conflicts with "
                "a source that already exists, with the core landing, with another "
                "source's match, or with another source's alias under the same profile, "
                "or because the deployed transform does not ship its variant (code "
                "missing_variant)"
            ),
        },
        422: {
            "model": CatalogueSourcesResponse,
            "description": (
                "Nothing was stored. The selection is longer than 50, an entry is not "
                "in the catalogue, does not arrive by the intake given, names a variant "
                "it does not compile to, or cannot be saved"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def create_sources_from_catalogue(
    body: CatalogueSourcesRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
    response: Response,
) -> CatalogueSourcesResponse:
    """Create a source for each catalogue entry, on the intake it arrives by.

    Each entry's file is the write body for its default intake, and another intake
    swaps in the match rule that intake derives. A variant the deployed transform's
    contract does not list is refused for that entry, and one whose contract is not
    mounted is allowed. Everything after that is the ordinary create, so each source
    is indistinguishable from a hand-written one.

    At most 50 entries. A longer selection is refused before anything is stored.
    Entries are stored in the order given. One entry's refusal is recorded in
    ``failed`` and the rest continue. The apps are reconciled once, after the
    entries that could be stored.
    """
    catalogue = _source_catalogue()
    created: list[CatalogueCreatedSource] = []
    failed: list[CatalogueSourceFailure] = []
    for item in body.entries:
        compiled = _compile_catalogue_item(catalogue, item, request=request)
        if isinstance(compiled, CatalogueSourceFailure):
            failed.append(compiled)
            continue
        write = compiled
        if registry.source_exists(write.source):
            failed.append(
                CatalogueSourceFailure(
                    entry=item.entry,
                    source=write.source,
                    code="conflict",
                    message=f"Source {write.source!r} already exists",
                )
            )
            continue
        try:
            source = registry.create_source_from_write(write, created_by=git_author(user))
        except SourceValidationError as exc:
            failed.append(_catalogue_failure(item.entry, exc, source=write.source))
            continue
        audit_resource_change(user.user_id, "source", source.source, "created")
        created.append(
            CatalogueCreatedSource(
                entry=item.entry,
                source=source.source,
                current=source.current,
                deployed_version=source.deployed_version,
                versions=sorted(source.versions.keys()),
            )
        )
    apps = AppsSync(changes=[])
    if created:
        apps = reconcile_derived_apps(request, user, registry)
    response.status_code = _catalogue_status(created, failed, apps_error=apps.error)
    return CatalogueSourcesResponse(
        created=created,
        failed=failed,
        apps_synced=apps.changes,
        apps_sync_error=apps.error,
        restart_required=apps.restart_required,
    )


@router.get(
    "/{name}/versions/{version}",
    response_model=SourceVersionGetDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def get_source_version(
    name: str,
    version: str,
    user: CurrentUser,
    registry: SourceReg,
):
    """Get one immutable source version snapshot by id, with build/deploy status."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        ) from None

    if version not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version}' not found for source '{name}'",
            },
        )

    store = SourceDeploymentStore.from_settings(get_settings())
    deploy_doc = store.load_deploy_document(source.source)
    version_ids = sorted(source.versions.keys())
    prev_deployed = previous_deployed_version_ids(source, deploy_doc)
    snap = source.versions[version]
    return SourceVersionGetDetailResponse(
        source=source.source,
        resource_type=source.resource_type,
        display_name=source.display_name,
        description=source.description,
        state=source.state,
        enabled=source.enabled,
        current=source.current,
        deployed_version=source.deployed_version,
        selected=version,
        versions=version_ids,
        previous_deployed_versions=prev_deployed,
        current_table_topic_type=source.table_topic_type,
        version=_version_detail_from_snapshot(source.source, version, snap, store),
    )


@router.get(
    "/{name}/columns",
    response_model=PaginatedResponse[SchemaColumn],
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def get_source_schema_columns(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
    version: str | None = Query(
        None,
        description="Source version id (defaults to deployed_version)",
    ),
    pagination: PaginationParams = Depends(),
) -> PaginatedResponse[SchemaColumn]:
    """Get composed schema columns for a source version (profile + meta/derived/additional).

    Use ``per_page=-1`` to return all columns in one page.
    """
    from dfe_engine.schema.schema_builder_v2 import (
        SchemaBuildError,
        SchemaBuilderV2,
        inherited_builder_kwargs,
    )
    from dfe_engine.schema.schema_loader import SchemaLoadError
    from dfe_engine.source.type_registry import TypeRegistry

    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{name}' not found"},
        ) from None

    version_id = version or source.runtime_version_id()
    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{name}'",
            },
        )

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    settings = ttl_settings(request, get_settings())
    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
        derived_base_dir=derived_reference_root(settings),
        **inherited_builder_kwargs(settings),
    )
    try:
        columns = builder.load_columns_for_source_version(source, source_version=version_id)
    # Both, and neither is redundant: the builder re-raises a profile or meta_schema
    # SchemaLoadError as SchemaBuildError, so which one arrives depends on where
    # the load failed. Narrowing to one answers 500 for the other.
    except (SchemaBuildError, SchemaLoadError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "schema_error", "message": str(exc)},
        ) from exc

    all_columns = [_to_api_schema_column(col) for col in columns]
    return PaginatedResponse.from_list(
        all_columns,
        pagination.page,
        pagination.per_page,
    )


@router.post(
    "/{name}/build",
    response_model=SchemaBuildResult,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
def build_source_schema(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
    version: str | None = Query(
        None,
        description="Source version id (defaults to deployed_version)",
    ),
) -> SchemaBuildResult:
    """Build complete schema (DDL) from a source version snapshot.

    Runs the v2 YAML -> DDL pipeline and returns the generated DDL
    without executing it against ClickHouse.
    """
    from dfe_engine.schema.schema_builder_v2 import SchemaBuildError, SchemaBuilderV2
    from dfe_engine.schema.schema_loader import SchemaLoadError

    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{name}' not found"},
        ) from None

    version_id = version or source.runtime_version_id()
    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{name}'",
            },
        )

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    settings = ttl_settings(request, get_settings())
    store = SourceDeploymentStore.from_settings(settings)
    try:
        result, _build_artifact = ensure_build_artifact(
            store,
            source,
            version_id=version_id,
            schemas_base_dir=settings.schemas.schemas_dir or None,
            refresh=True,
            settings=settings,
            field_map_registry=get_field_map_registry_optional(),
        )
    except (SchemaBuildError, SchemaLoadError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    columns = [_to_api_schema_column(col) for col in result.columns]

    ddl = None
    if result.create_table_ddl:
        ddl = DDLResult(
            source_name=name,
            create_table=result.create_table_ddl,
            views={k: v for k, v in (result.view_ddls or {}).items()},
        )

    audit_resource_change(user.user_id, "schema", name, "executed")
    return SchemaBuildResult(
        source_name=name,
        version=version_id,
        columns=columns,
        ddl=ddl,
        validation_errors=list(result.validation_errors),
    )


@router.post(
    "/{name}/plan",
    response_model=SourcePlanResponse,
    dependencies=[Depends(require_action(scopes_dict["source_deploy"]))],
)
def plan_source_deploy(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    ch_client: ClickHouseClient,
    request: Request,
    version: str | None = Query(
        None,
        description="Source version id (defaults to current working version)",
    ),
) -> SourcePlanResponse:
    """Dry-run ClickHouse deploy: DDL statements and validation errors (not persisted)."""
    from dfe_engine.schema.plan import platform_probe
    from dfe_engine.schema.schema_builder_v2 import (
        SchemaBuildError,
        SchemaBuilderV2,
        inherited_builder_kwargs,
    )
    from dfe_engine.schema.schema_loader import SchemaLoadError
    from dfe_engine.source.type_registry import TypeRegistry

    source, version_id = _resolve_source_version(registry, name, version, default_current=True)
    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    settings = ttl_settings(request, get_settings())
    store = SourceDeploymentStore.from_settings(settings)
    # The plan shows the DDL the deploy would apply, so it probes the same server.
    platform = platform_probe(settings, ch_client).platform
    try:
        result, _artifact = ensure_build_artifact(
            store,
            source,
            version_id=version_id,
            schemas_base_dir=settings.schemas.schemas_dir or None,
            refresh=True,
            platform=platform,
            settings=settings,
            field_map_registry=get_field_map_registry_optional(),
        )
    except (SchemaBuildError, SchemaLoadError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
        derived_base_dir=derived_reference_root(settings),
        platform=platform,
        **inherited_builder_kwargs(settings),
    )
    db = settings.clickhouse.effective_data_database
    try:
        planned = plan_deploy_statements(
            builder,
            source,
            version_id,
            result,
            db=db,
            ch_client=ch_client,
        )
    except SchemaApplyError as exc:
        raise _ch_unreadable(exc, name) from exc
    plan = plan_from_build(
        result,
        version=version_id,
        statements=planned.statements,
        table_exists=planned.table_exists,
        ttl_change=planned.ttl_change,
    )
    audit_resource_change(user.user_id, "source", name, "planned")
    return _plan_to_response(plan)


class _TopicsNotCreatedError(Exception):
    """A source riding the bus could not get its topics from the broker, so it must not deploy.

    Attributes:
        failed: Each topic not created, with the reason ``ensure_topics`` recorded.
        ensured: The topics that were created or already existed.
    """

    def __init__(self, *, failed: list[tuple[str, str]], ensured: list[str]) -> None:
        """Record what the broker refused and what it took."""
        super().__init__(", ".join(name for name, _ in failed))
        self.failed = failed
        self.ensured = ensured


class _SourceTopics(NamedTuple):
    """What a deploy did to its source's Kafka topics."""

    ensured: list[str]
    failed: list[str]
    removed: list[str]
    pending: list[str]


def _rides_bus(source: Any, settings: Any, version_id: str | None) -> bool:
    """Whether *source* at this version, else its deployed one, moves its records over the bus."""
    declared = source.version(version_id).transport if version_id else source.transport
    return settings.transport.bus_present and (declared or settings.transport.default) == "bus"


def transform_instances(gc: Any) -> set[str] | None:
    """The instance names of every transform app in the deploy repo, or None without one.

    A transform instance is named for the source whose ``_load`` topic it writes.
    """
    if gc is None:
        return None
    return {app.instance for app in instances.instances_routed_by(gc, TRANSFORM_COMPILER)}


def _owner(source: Any, registry: Any) -> Any:
    """The source whose ``_load`` topic *source* is judged on, or None where none is registered.

    That is the source *source* lands under, which is *source* itself unless it
    lands on another's topic.
    """
    label = source.landing_label()
    if label == source.source:
        return source
    if registry is None or not registry.source_exists(label):
        return None
    return registry.get_source(label)


def _load_topic_made(source: Any, registry: Any) -> bool:
    """Whether the engine ever created the ``_load`` topic *source* is judged on.

    The engine creates it for each version of the topic's owner with a transform.
    """
    owner = _owner(source, registry)
    return owner is not None and any(version.transform for version in owner.versions.values())


def _writer_groups(source: Any, registry: Any) -> list[str]:
    """The consumer group of every transform that can have written *source*'s ``_load`` topic.

    One for each transform engine a version of the topic's owner names. Empty
    where any of them has no group the app manifest can name, which holds the topic.
    """
    owner = _owner(source, registry)
    if owner is None:
        return []
    engines = {version.transform.engine for version in owner.versions.values() if version.transform}
    groups: list[str] = []
    for engine in sorted(engines):
        try:
            app = app_catalogue.descriptor(app_catalogue.transform_service(engine))
        except app_catalogue.UnknownAppError:
            return []
        group = app_catalogue.transform_group(app, owner.source)
        if not group:
            return []
        groups.append(group)
    return groups


def _settle_stranded(
    source: Any,
    topics: list[str],
    *,
    transforms: set[str] | None,
    admin: Any,
    settings: Any,
    registry: Any,
) -> tuple[list[str], list[str]]:
    """Delete each stranded topic nothing writes and nothing has left to read, and keep the rest.

    ``transforms`` is the transform instances the deploy repo carries. None means
    there is no deploy repo, so whether a transform still writes the topic cannot
    be seen and every topic is kept. An instance gone from the deploy repo can
    still be running, so its transform's consumer group must also be idle.

    Returns:
        The topics removed and the ones pending removal.
    """
    from dfe_engine.kafka.topics import build_admin, clear_stranded_topics

    if transforms is None or source.landing_label() in transforms:
        logger.info(
            "stranded topic kept while a transform instance may still write it",
            source=source.source,
            topics=",".join(topics),
        )
        return [], list(topics)
    cleared = clear_stranded_topics(
        topics,
        admin=admin or build_admin(settings=settings),
        writers=_writer_groups(source, registry),
    )
    for topic in cleared.held:
        logger.info(
            "stranded topic kept", source=source.source, topic=topic.name, reason=topic.reason
        )
    if cleared.removed:
        logger.info(
            "stranded topic removed so the loader reads the source's landing topic",
            source=source.source,
            removed=",".join(cleared.removed),
        )
    return cleared.removed, [topic.name for topic in cleared.held]


def sweep_stranded_topics(
    registry: Any, settings: Any, transforms: set[str] | None, *, actor: str
) -> tuple[list[str], list[str]]:
    """Settle the stranded ``_load`` topic of every live source on the bus. Never raises.

    Only a source whose ``_load`` topic the engine ever created is looked up, so a
    deployment that never dropped a transform makes no broker call here.
    ``transforms`` is as :func:`_settle_stranded` takes it, and ``actor`` is who
    each deletion is audited as.

    Returns:
        The topics removed and the ones still pending removal.
    """
    from dfe_engine.kafka.topics import build_admin, stranded_source_topics, topics_managed

    removed: list[str] = []
    pending: list[str] = []
    try:
        if not topics_managed(settings):
            return removed, pending
        live = [
            source
            for source in registry.get_all_sources(states=("active",))
            if source.deployed_version
            and _rides_bus(source, settings, None)
            and _load_topic_made(source, registry)
        ]
        admin = build_admin(settings=settings) if live else None
        for source in live:
            found = stranded_source_topics(source, settings=settings, admin=admin) or []
            # Sources landing under one label share its topic, so it is settled once.
            topics = [topic for topic in found if topic not in removed + pending]
            if not topics or _another_transform_writes(source, registry):
                continue
            gone, kept = _settle_stranded(
                source,
                topics,
                transforms=transforms,
                admin=admin,
                settings=settings,
                registry=registry,
            )
            if gone:
                audit_resource_change(
                    actor,
                    "schema",
                    source.source,
                    "topics_removed",
                    details={"topics_removed": gone},
                )
            removed += gone
            pending += kept
    except Exception as exc:  # the apps are reconciled whatever the broker answers
        logger.warning("stranded topics not settled", error=str(exc))
    return removed, pending


def _another_transform_writes(source: Any, registry: Any) -> bool:
    """Whether another source's running transform still writes the ``_load`` topic *source* is judged on.

    A source landing on another's topic is judged on that source's ``_load`` one. The
    engine runs a transform only for an active, deployed source, so only such an
    owner still writes it.
    """
    owner = source.landing_label()
    if registry is None or owner == source.source or not registry.source_exists(owner):
        return False
    other = registry.get_source(owner)
    if not (other.enabled and other.deployed_version):
        return False
    return bool(other.version(other.deployed_version).transform)


def _ensure_source_topics(
    source: Any,
    settings: Any,
    version_id: str | None = None,
    registry: Any = None,
    gc: Any = None,
) -> _SourceTopics:
    """Create the ``_land``/``_load`` topics this source needs, and settle a stranded one.

    Fatal where the source rides the bus: its receiver answers 503 for every
    record until the topic exists, so a deploy reporting success would be wrong.
    Off the bus (a brokerless or direct deployment) no stage reads the topics,
    and a failure is reported on the response instead.

    On the bus a ``_load`` topic left by a version that had a transform stops the
    loader reading the source's ``_land`` one. It is deleted here only where the
    deploy repo ``gc`` already carries no transform instance writing it, the
    transform's consumer group holds no live member, and no consumer group has
    records left to read on it. Otherwise it is reported as pending for a later
    apps reconcile to delete. The deploy goes ahead either way.

    Blocking librdkafka calls run in here, so callers on the event loop hand it to
    a thread. ``version_id`` is the version being deployed, not the deployed one.
    ``registry`` names the topic's owner and its transforms.

    Returns:
        The topics ensured, the ones not created, the stranded ones removed and
        the stranded ones pending removal.

    Raises:
        _TopicsNotCreatedError: The source rides the bus and a topic was not created.
    """
    from dfe_engine.kafka.topics import (
        build_admin,
        deployment_topic_config,
        ensure_topics,
        source_replication_factor,
        source_topic_specs,
        stranded_source_topics,
        topics_managed,
    )

    if not topics_managed(settings):
        return _SourceTopics([], [], [], [])

    # One admin for every pass. Each builds its own otherwise, and an unreachable
    # broker then costs another full librdkafka timeout on the request path.
    try:
        admin = build_admin(settings=settings)
    except Exception:
        admin = None

    specs = source_topic_specs(
        source,
        partitions=settings.kafka.topic_partitions,
        replication_factor=source_replication_factor(settings, admin=admin),
        config=deployment_topic_config(settings),
        version_id=version_id,
    )
    outcome = ensure_topics(specs, admin=admin, settings=settings)
    ensured = outcome.created + outcome.existing
    rides_bus = _rides_bus(source, settings, version_id)
    if outcome.failed:
        logger.warning(
            f"Kafka topics not ensured for source '{source.source}': "
            f"{', '.join(f'{n} ({e})' for n, e in outcome.failed)}"
        )
        if rides_bus:
            raise _TopicsNotCreatedError(failed=outcome.failed, ensured=ensured)
    removed: list[str] = []
    pending: list[str] = []
    if rides_bus:
        # None means the broker could not be asked, which the ensure above has already refused.
        stranded = stranded_source_topics(
            source, version_id=version_id, settings=settings, admin=admin
        )
        if stranded and not _another_transform_writes(source, registry):
            removed, pending = _settle_stranded(
                source,
                stranded,
                transforms=transform_instances(gc),
                admin=admin,
                settings=settings,
                registry=registry,
            )
    return _SourceTopics(ensured, [name for name, _ in outcome.failed], removed, pending)


def _remove_source_topics(source: Any, settings: Any) -> tuple[list[str], list[str]]:
    """Delete the ``_land``/``_load`` topics this source's deploys created.

    The same dial that creates them removes them: an engine that manages a
    source's topics manages them for the source's whole life, and leaving them
    costs a partition assignment in the loader for a source nothing can write to.

    Non-fatal like the ensure, and blocking, so a caller on the event loop hands
    it to a thread. Returns the topics removed and the ones that could not be.
    """
    from dfe_engine.kafka.topics import remove_topics, source_topic_names, topics_managed

    if not topics_managed(settings):
        return [], []

    outcome = remove_topics(source_topic_names(source), settings=settings)
    if outcome.failed:
        logger.warning(
            f"Kafka topics not removed for source '{source.source}': "
            f"{', '.join(f'{n} ({e})' for n, e in outcome.failed)}"
        )
    return outcome.removed, [name for name, _ in outcome.failed]


_SOURCE_DEPLOY_TASK_KIND = "source:deploy"


async def _execute_source_deploy(
    *,
    name: str,
    user: Any,
    registry: Any,
    settings: Any,
    request: Request,
    version: str | None,
    dry_run: bool,
) -> SchemaDeployResult:
    """Plan or deploy one source version. Shared by the single and bulk deploy paths."""
    ddl_settings = await run_blocking(functools.partial(ttl_settings, request, settings))
    applied = await run_blocking(
        functools.partial(_apply_source_schema, name, registry, ddl_settings, version, dry_run)
    )
    if isinstance(applied, SchemaDeployResult):
        return applied
    source, version_id, result, views, statements_applied, db, ttl_change = applied
    await _rebuild_all_sources(registry=registry, settings=settings, sources=[name])

    # Off the event loop: the admin calls block for their full timeout when no
    # broker answers, which is the norm on the Kafka-less profile.
    gc = getattr(request.app.state, "gitcrud", None)
    try:
        topics = await asyncio.to_thread(
            _ensure_source_topics, source, settings, version_id, registry, gc
        )
    except _TopicsNotCreatedError as exc:
        # The DDL and any topic created before the refusal changed state, so the attempt is audited.
        audit_resource_change(
            user.user_id,
            "schema",
            name,
            "deploy_failed",
            details={
                "version": version_id,
                "statements_applied": statements_applied,
                "topics_ensured": exc.ensured,
                "topics_failed": [topic for topic, _ in exc.failed],
            },
        )
        raise _topics_not_created(name, exc.failed) from exc

    deploy_result = SchemaDeployResult(
        source_name=name,
        version=version_id,
        dry_run=False,
        applied=True,
        create_table=result.create_table_ddl,
        views=views,
        validation_errors=[],
        statements_applied=statements_applied,
        topics_ensured=topics.ensured,
        topics_failed=topics.failed,
        topics_removed=topics.removed,
        topics_pending_removal=topics.pending,
        ttl_change=ttl_change,
    )
    store = SourceDeploymentStore.from_settings(settings)
    apps = await run_blocking(
        functools.partial(
            _record_source_deploy,
            store,
            deploy_result,
            source,
            name,
            version_id,
            user,
            registry,
            request,
        )
    )

    # The HyperDX source is how an operator sees the rows the new table takes;
    # without it the deploy lands and stays invisible until someone adds one.
    hyperdx_teams, hyperdx_error = await _sync_hyperdx_source(
        request, source, db, [col.name for col in result.columns]
    )
    await _register_hyperdx_all_sources(database=db, request=request)

    deploy_result = deploy_result.model_copy(
        update={
            "apps_synced": apps.changes,
            "apps_sync_error": apps.error,
            "restart_required": apps.restart_required,
            "hyperdx_source_teams": hyperdx_teams,
            "hyperdx_source_error": hyperdx_error,
        }
    )
    await run_blocking(functools.partial(store.save_deploy, deploy_result, source))

    # Topic creation mutates the broker, so it is attributable and belongs in the
    # audit record alongside the DDL rather than only in an unattributed log line.
    audit_resource_change(
        user.user_id,
        "schema",
        name,
        "deployed",
        details={
            "version": version_id,
            "topics_ensured": topics.ensured,
            "topics_failed": topics.failed,
            "topics_removed": topics.removed,
            "topics_pending_removal": topics.pending,
            "apps_synced": apps.changes,
        },
    )
    # Every stranded-topic deletion is audited under one action, whichever path made it.
    if topics.removed:
        audit_resource_change(
            user.user_id,
            "schema",
            name,
            "topics_removed",
            details={"topics_removed": topics.removed},
        )
    return deploy_result


def _topics_not_created(name: str, failed: list[tuple[str, str]]) -> HTTPException:
    """The 502 for a deploy whose source rides the bus and did not get every topic.

    Each reason is the one ``ensure_topics`` recorded, which names the broker's
    error code and never its text.
    """
    named = ", ".join(f"{topic} ({reason})" for topic, reason in failed)
    return HTTPException(
        status_code=502,
        detail={
            "code": TOPICS_NOT_CREATED,
            "message": (
                f"Kafka topics not created for source '{name}', so it was not deployed: "
                f"{named}. Its table is in place. Deploy again once the broker takes the topics"
            ),
            "source": name,
            "topics": [{"topic": topic, "reason": reason} for topic, reason in failed],
        },
    )


def _http_exception_message(exc: HTTPException) -> str:
    """A task-facing message from an HTTPException detail."""
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    return str(detail)


async def _run_source_deploy_task(
    name: str,
    version: str | None,
    dry_run: bool,
    user: Any,
    registry: Any,
    settings: Any,
    request: Request,
    *,
    task,
) -> dict[str, Any]:
    """Background worker for one source deploy (bulk ``action=deploy``)."""
    label = f"{name}" + (f"@{version}" if version else "")
    task.set_progress(5, f"{'Planning' if dry_run else 'Deploying'} {label}")
    try:
        result = await _execute_source_deploy(
            name=name,
            user=user,
            registry=registry,
            settings=settings,
            request=request,
            version=version,
            dry_run=dry_run,
        )
    except HTTPException as exc:
        raise RuntimeError(_http_exception_message(exc)) from exc
    task.set_progress(100, f"{'Planned' if dry_run else 'Deployed'} {label}")
    return result.model_dump(mode="json")


@router.post(
    "/{name}/deploy",
    response_model=SchemaDeployResult,
    responses={
        502: {
            "model": TopicsNotCreatedErrorResponse | ErrorResponse,
            "description": (
                "A backing service refused the deploy: ClickHouse rejected the DDL, or "
                "the source rides the bus and the broker did not create its topics "
                "(code topics_not_created, naming each topic and the broker's error code)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def deploy_source_schema(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    request: Request,
    version: str | None = Query(
        None, description="Source version id (defaults to the source's current version)"
    ),
    dry_run: bool = Query(
        False, description="Plan only: generate + validate the DDL without applying it"
    ),
) -> SchemaDeployResult:
    """Plan (``dry_run=true``) or deploy a source version's schema to ClickHouse.

    Runs the v2 YAML -> DDL pipeline. In plan mode the CREATE TABLE (+ any standard
    views) and validation errors are returned for review WITHOUT touching
    ClickHouse. In deploy mode the DDL is applied: an absent table is created, and
    an existing one gains the columns it lacks and moves to the version's TTL, so a
    re-deploy changes only what differs. The engine and the type of an existing
    column are left as created. A schema that failed validation is never deployed.
    A source on the bus transport whose Kafka topics the broker did not create is
    not marked deployed either, and the call answers 502 naming each topic.
    """
    return await _execute_source_deploy(
        name=name,
        user=user,
        registry=registry,
        settings=settings,
        request=request,
        version=version,
        dry_run=dry_run,
    )


class _AppliedSchema(NamedTuple):
    """A source version whose DDL has reached ClickHouse, and what it took."""

    source: Source
    version_id: str
    result: Any
    views: dict[str, str]
    statements_applied: int
    database: str
    ttl_change: TtlChange | None


def _record_source_deploy(
    store: SourceDeploymentStore,
    deploy_result: SchemaDeployResult,
    source: Source,
    name: str,
    version_id: str,
    user: Any,
    registry: Any,
    request: Request,
) -> AppsSync:
    """Record the deploy, mark the version deployed and reconcile the apps it makes live."""
    store.save_deploy(deploy_result, source)
    try:
        registry.set_deployed_version(
            name,
            version_id,
            created_by=git_author(user),
            description=f"source: deploy {name} version {version_id}",
        )
    except SourceValidationError as exc:
        _raise_save_validation_http(exc)

    # The receiver's rule for this source, the loader's table map and (for a
    # fetcher-based source) the fetcher instance are what make the deploy live.
    return reconcile_derived_apps(request, user, registry)


def _dry_run_table_moves(
    builder: Any, source: Source, version_id: str, result: Any, settings: Any
) -> tuple[TtlChange | None, SettingsChange | None, str | None]:
    """The TTL and settings moves a deploy would make on the source's table, and why unknown.

    Only a deployed source has a table to move. The table is read within
    ``clickhouse.interactive_budget_seconds``; a ClickHouse that cannot be read
    leaves both moves unknown and says so.
    """
    if source.deployed_version is None:
        return None, None, None
    db = settings.clickhouse.effective_data_database
    client = get_interactive_clickhouse_client(settings)
    try:
        live = live_tables(client, db).get(source.table_name)
        if live is None:
            return None, None, None
        settings_change = settings_change_for_table(builder, source, result, db=db, client=client)
    except SchemaApplyError as exc:
        return (
            None,
            None,
            hide_backend_text(
                _CH_UNREADABLE, exc, event="dry run: live table unreadable", source=source.source
            ),
        )
    ttl_change = ttl_change_for_table(builder, source, version_id, result, db=db, live=live.ttl)
    return ttl_change, settings_change, None


def _plan_and_apply_ddl(
    builder: Any,
    source: Source,
    version_id: str,
    result: Any,
    *,
    ch: Any,
    db: str,
    name: str,
    platform: Any,
) -> tuple[DeployStatements, int]:
    """Plan a source version's DDL against the live table, then apply it.

    Returns:
        The plan, and how many of its statements ClickHouse took.

    Raises:
        HTTPException: 503 when ClickHouse could not be read, 502 when it rejected a statement.
    """
    try:
        planned = plan_deploy_statements(
            builder,
            source,
            version_id,
            result,
            db=db,
            ch_client=ch,
        )
    except SchemaApplyError as exc:
        raise _ch_unreadable(exc, name) from exc
    applied = 0
    try:
        # Through the applier, which creates the Replicated database every
        # replica holds rather than a plain one on the replica this connection reached.
        from dfe_engine.schema.applier import SchemaApplier

        SchemaApplier(ch, platform).ensure_database(db)
        for stmt in planned.statements:
            execute_ddl(ch, stmt)
            applied += 1
    except Exception as exc:
        raise backend_failure(
            502,
            "ddl_apply_error",
            f"ClickHouse rejected DDL after {applied} statement(s); the engine log has "
            "ClickHouse's reason",
            exc,
            event="source deploy: ClickHouse rejected DDL",
            source=name,
            applied=applied,
        ) from exc
    return planned, applied


def _apply_source_schema(
    name: str, registry: Any, settings: Any, version: str | None, dry_run: bool
) -> SchemaDeployResult | _AppliedSchema:
    """Build a source version's DDL and, unless a dry run, apply it to ClickHouse.

    A dry run returns its finished result; a deploy returns what the caller needs to
    ensure the topics and record the deploy.
    """
    from dfe_engine.schema.schema_builder_v2 import (
        SchemaBuildError,
        SchemaBuilderV2,
        inherited_builder_kwargs,
    )
    from dfe_engine.schema.schema_loader import SchemaLoadError
    from dfe_engine.source.type_registry import TypeRegistry

    # Targets current: defaulting to the deployed version makes an unparameterised
    # deploy a no-op that can never advance.
    source, version_id = _resolve_source_version(registry, name, version, default_current=True)

    snap = source.versions[version_id]
    if not SchemaBuilderV2.version_snapshot_has_schema_files(snap):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_schema",
                "message": f"Source '{name}' version '{version_id}' has no schema configured",
            },
        )

    # A deploy ends by recording the version, which a core source refuses, so refuse
    # it before any DDL or topic lands for a deploy that could never be recorded.
    if not dry_run and registry.is_core(name):
        _raise_save_validation_http(SourceCoreResourceError(action="deploy", source=name))

    # A deploy reaches ClickHouse before the build, so the platform is probed on the
    # server the DDL lands on. A dry run only reads ClickHouse, and takes the
    # platform the hostname names.
    from dfe_engine.schema.plan import platform_probe

    ch = None
    if not dry_run:
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
        from dfe_engine.settings import get_clickhouse_config

        try:
            ch = ClickHouseManager.get_instance(
                get_clickhouse_config(settings)
            ).get_clickhouse_client()
        except Exception as exc:
            raise _ch_unreadable(exc, name) from exc
    probe = platform_probe(settings, ch)
    platform = probe.platform

    builder = SchemaBuilderV2(
        TypeRegistry.default(),
        schemas_base_dir=settings.schemas.schemas_dir or None,
        derived_base_dir=derived_reference_root(settings),
        platform=platform,
        field_map_registry=get_field_map_registry_optional(),
        **inherited_builder_kwargs(settings),
    )
    try:
        result = builder.build_for_source_version(source, source_version=version_id)
    # Both, and neither is redundant: the builder re-raises a profile or meta_schema
    # SchemaLoadError as SchemaBuildError, so which one arrives depends on where
    # the load failed. Narrowing to one answers 500 for the other.
    except (SchemaBuildError, SchemaLoadError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "build_error", "message": str(exc)},
        ) from exc

    if not result.create_table_ddl:
        raise HTTPException(
            status_code=400,
            detail={"code": "no_ddl", "message": "no CREATE TABLE DDL was generated"},
        )

    views = dict(result.view_ddls or {})
    validation_errors = list(result.validation_errors or [])

    # Plan mode: hand the DDL + validation back for review; never write to ClickHouse.
    if dry_run:
        ttl_change, settings_change, live_table_error = _dry_run_table_moves(
            builder, source, version_id, result, settings
        )
        return SchemaDeployResult(
            source_name=name,
            version=version_id,
            dry_run=True,
            applied=False,
            create_table=result.create_table_ddl,
            views=views,
            validation_errors=validation_errors,
            ttl_change=ttl_change,
            settings_change=settings_change,
            live_table_error=live_table_error,
        )

    # Deploy mode: refuse to apply a schema that failed validation.
    if validation_errors:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "validation_failed",
                "message": "schema has validation errors; fix them or use dry_run to review",
                "errors": validation_errors,
            },
        )

    # The schema phase's refusal, made here too: after the caller's own errors, which
    # need no server, and before the first statement reaches one.
    _require_replication(probe, name)
    db = settings.clickhouse.effective_data_database
    # Planned under the lease too, so the columns it diffs against are the ones the DDL meets.
    try:
        with ddl_lease(
            ch, data_database=db, wait_seconds=settings.clickhouse.bootstrap_wait_seconds
        ):
            planned, applied = _plan_and_apply_ddl(
                builder, source, version_id, result, ch=ch, db=db, name=name, platform=platform
            )
    except SchemaLockBusyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "schema_locked",
                "message": (
                    "another schema apply held the ClickHouse schema lease for the whole "
                    "wait, so retry once it finishes"
                ),
                "holder": exc.holder,
            },
        ) from exc
    except SchemaLockError as exc:
        raise _ch_unreadable(exc, name) from exc

    # The tier grant is database-wide, so the new table is readable the moment it
    # exists while its row policy would otherwise wait for the next reconcile.
    try:
        from dfe_engine.governance.ch.reconciler import fence_tables

        # Only a dry run leaves ch unset, and a dry run has returned before this.
        fence_tables(ch._client, database=db)  # ty: ignore[unresolved-attribute]
    except Exception as exc:
        logger.warning(f"Tenant fence not applied after deploying '{name}': {exc}")

    return _AppliedSchema(source, version_id, result, views, applied, db, planned.ttl_change)


@router.get(
    "/{name}",
    response_model=SourceDetailResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def get_source(name: str, user: CurrentUser, registry: SourceReg):
    """Get a full source definition by name, including build/deploy per version."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        ) from None
    store = SourceDeploymentStore.from_settings(get_settings())
    return _to_source_detail_response(source, store)


@router.get(
    "/{name}/flow",
    response_model=SourceFlowResponse,
    responses={
        422: {
            "description": (
                "The source's stages cannot be run as declared - the message names "
                "which stage refused and why"
            )
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def get_source_flow(name: str, user: CurrentUser, registry: SourceReg, settings: Settings):
    """The stages this source's records travel, resolved against this deployment.

    The console draws the flow from this rather than from the source's fields,
    for the same reason the compilers write from it: the topics, the endpoints
    and the instance running each stage follow from the source plus the
    deployment, and one resolver is what keeps the drawing and the deployed
    config the same answer.
    """
    source = _resolve_source(name, registry)
    try:
        return _flow_response(resolve_flow(source, settings))
    except FlowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "flow_error", "message": str(exc)},
        ) from exc


@router.get(
    "/{name}/signals",
    response_model=SourceSignalsResponse,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def get_source_signals(
    name: str,
    user: CurrentUser,
    request: Request,
    registry: SourceReg,
    settings: Settings,
    client: ClickHouseClient,
) -> SourceSignalsResponse:
    """Whether this source's records are arriving, and how fast.

    Reads the loader's per-table counter out of the otel tables over a bounded
    five-minute window, so the console can put a number beside a source without
    the operator opening HyperDX. It answers 200 with nulls where the series is
    absent: a source that was only just defined has nothing to report, and that
    is not an error.
    """
    source = _resolve_source(name, registry)
    table, in_default = _landing_table(source)
    loaders = _deployed_loaders(request)
    try:
        signals = OperationalReader(
            client, settings.clickhouse.effective_data_database
        ).source_signals(table, loaders)
    except MetricsUnavailableError as exc:
        raise metrics_unavailable(exc) from exc
    return SourceSignalsResponse(
        source=source.source,
        table=table,
        landed_in_default=in_default,
        window_seconds=signals.window_seconds,
        records_per_min=signals.records_per_min,
        last_seen=(
            datetime.fromtimestamp(signals.last_seen_epoch, tz=UTC).isoformat()
            if signals.last_seen_epoch is not None
            else None
        ),
    )


@router.get(
    "/{name}/export",
    response_model=SourceBundle,
    response_model_exclude_none=True,
    dependencies=[Depends(require_action(scopes_dict["source_read"]))],
)
def export_source(
    name: str,
    user: CurrentUser,
    registry: SourceReg,
    schema_registry: SchemaReg,
    version: str | None = Query(
        None, description="Version id to export; omitted takes the source's current"
    ),
) -> SourceBundle:
    """Export one source version as a bundle another deployment can import.

    The bundle carries the version's routing, schema and transform, each only
    where the source declares it, plus one document per meta schema the schema
    pins name. A pinned schema with ``resource_type: core`` travels as a
    REFERENCE and a version pin rather than a copy of its columns, so importing
    the bundle cannot fork the read-only definition dfe-schemas ships.
    """
    _resolve_source(name, registry)
    try:
        return build_source_bundle(registry, schema_registry, name, version=version)
    except ExchangeError as exc:
        raise_exchange_http(exc)


@router.put(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": SourceWriteConflictResponse,
            "description": (
                "Receiver match duplicates another enabled source (code match_conflict), "
                "an alias is another source's under the same profile (code alias_conflict), "
                "or the source is engine-owned and no write path may change it (code conflict)"
            ),
        },
        **_INVALID_RULE,
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def update_source(
    name: str,
    body: SourceWriteRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
):
    """Update a source from a flat revision body.

    Before the first deploy, edits update the working version in place. After deploy, a new
    major version is created only when ``current`` equals ``deployed_version`` and a
    table-shaping field changes: schema pins (``meta_schema``, ``meta_schema_version``,
    ``derived_schema``, ``additional_fields``), ``ttl_days``, ``engine``, common-header
    type/version, ``views``, or ``transform``. Draft versions (``current`` not deployed)
    update in place.

    Origin (receiver vs fetcher) cannot change, and a pinned ``meta_schema`` cannot be
    cleared: once a source owns its own table it cannot move back onto ``main``.

    A field the body omits keeps its stored value (``header``, ``schema``, ``views``,
    ``transform``, ``transport``, ``archive``, ``aliases``, ``compat_profiles``), and
    ``null``, ``[]`` or ``false`` clears it. ``compat_profiles: null`` returns the source
    to the deployment's profiles.

    A schema pin naming nothing is accepted here so a source can be written before
    its schema, while ``/sources/import`` refuses one because an exported bundle
    brings every schema it pins.
    """
    from dfe_engine.source.registry import SourceNotFoundError

    if not registry.source_exists(name):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        )
    _refuse_unknown_fetched_org(request, body)

    try:
        source = registry.update_source_from_write(name, body, created_by=git_author(user))
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source '{name}' not found",
            },
        ) from None
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    audit_resource_change(user.user_id, "source", source.source, "updated")
    return _source_response(
        source, message="updated", apps=reconcile_derived_apps(request, user, registry)
    )


@router.patch(
    "/{name}",
    response_model=SourceResponse,
    responses={
        409: {
            "model": SourceWriteConflictResponse,
            "description": (
                "Enabling would duplicate another enabled source's receiver match (code "
                "match_conflict) or alias (code alias_conflict), or the source is "
                "engine-owned and no write path may change it (code conflict)"
            ),
        },
        **_INVALID_RULE,
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def patch_source_enabled(
    name: str,
    body: SourceEnabledPatchRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
):
    """Set a source's lifecycle state without changing versioned configuration.

    Does not create a new source version. Activating may return ``409
    match_conflict`` if another non-disabled source already uses the same
    receiver match rule.
    """
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        ) from None

    target = body.target_state()
    if source.state == target:
        return _source_response(source, message=target)

    updated = source.model_copy(update={"state": target})
    try:
        saved = registry.save_source(
            updated,
            created_by=git_author(user),
            description=f"source: set {name} {target}",
        )
    except SourceValidationError as e:
        _raise_save_validation_http(e)

    audit_resource_change(user.user_id, "source", saved.source, target)
    return _source_response(
        saved, message=target, apps=reconcile_derived_apps(request, user, registry)
    )


@router.delete(
    "/{name}",
    response_model=SourceDeleteResponse,
    responses={
        409: {
            "model": CoreResourceConflictErrorResponse,
            "description": "The source is engine-owned and no write path may delete it",
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_delete"]))],
)
async def delete_source(
    name: str, user: CurrentUser, registry: SourceReg, settings: Settings, request: Request
) -> SourceDeleteResponse:
    """Delete a source by name. Its fetcher instance, receiver rule and topics go with it.

    An engine-owned source -- the landing table's own -- is refused with 409
    ``conflict``, the same answer PUT and PATCH give.

    The answer reports the reconcile as a create or an update does: on a
    deployment that renders the app config itself, ``restart_required`` names the
    command that takes away the containers the source's instances ran in.
    """
    source = await run_blocking(
        functools.partial(_delete_stored_source, name, user, registry, settings)
    )
    # Off the event loop: the admin calls block for their full timeout when no
    # broker answers, which is the norm on the Kafka-less profile.
    topics_removed, topics_failed = await asyncio.to_thread(_remove_source_topics, source, settings)
    # Deleting a topic destroys what is on it, so it is attributable and belongs
    # in the audit record rather than only in a log line.
    audit_resource_change(
        user.user_id,
        "source",
        name,
        "deleted",
        details={"topics_removed": topics_removed, "topics_failed": topics_failed},
    )
    apps = await run_blocking(functools.partial(reconcile_derived_apps, request, user, registry))
    await _remove_hyperdx_source(request, name)
    # The table stays in ClickHouse; leaving the registry is what takes it out of all_sources.
    await _rebuild_all_sources(registry=registry, settings=settings, sources=[name])
    return SourceDeleteResponse(
        source=name,
        apps_synced=apps.changes,
        apps_sync_error=apps.error,
        restart_required=apps.restart_required,
    )


def _delete_stored_source(name: str, user: Any, registry: Any, settings: Any) -> Source:
    """Delete one source and its deployment records and return it as it was, or 404."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Source {name!r} not found",
            },
        ) from None
    try:
        registry.delete_source(
            name,
            created_by=git_author(user),
            deployment_store=SourceDeploymentStore.from_settings(settings),
        )
    except SourceValidationError as e:
        _raise_save_validation_http(e)
    return source


@router.post(
    "/bulk",
    response_model=BulkActionResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
async def bulk_action(
    body: BulkActionRequest,
    user: CurrentUser,
    registry: SourceReg,
    settings: Settings,
    request: Request,
    response: Response,
):
    """Perform a bulk action on multiple sources.

    ``enable`` / ``disable`` / ``dormant`` / ``delete`` apply immediately.
    ``deploy`` submits one ``source:deploy`` background task per accepted source
    (poll ``GET /tasks/{task_id}``); refused names land in ``failed``.

    An action that applies immediately reports the one reconcile it ends with, as
    a single-source write does.
    """
    action_to_state = {"enable": "active", "disable": "disabled", "dormant": "dormant"}
    if body.action not in (*action_to_state, "delete", "deploy"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": (
                    f"Unknown action {body.action!r}. Must be: enable, disable, "
                    "dormant, delete, deploy"
                ),
            },
        )

    if body.action == "deploy":
        response.status_code = 202
        return _submit_bulk_deploys(body, user, registry, settings, request)

    succeeded, failed, deleted = await run_blocking(
        functools.partial(_apply_bulk, body, action_to_state, user, registry, settings)
    )

    apps = AppsSync(changes=[])
    if succeeded:
        audit_resource_change(user.user_id, "source", ",".join(succeeded), body.action)
        apps = await run_blocking(
            functools.partial(reconcile_derived_apps, request, user, registry)
        )
        for source in deleted:
            # One pass per source: the delete is already done, and a broker that
            # cannot be reached must not cost the next source its cleanup.
            await asyncio.to_thread(_remove_source_topics, source, settings)
        if body.action == "delete":
            for name in succeeded:
                await _remove_hyperdx_source(request, name)
            # Once for the batch: each rebuild reads the whole registry.
            await _rebuild_all_sources(registry=registry, settings=settings, sources=succeeded)
    return BulkActionResponse(
        action=body.action,
        succeeded=succeeded,
        failed=failed,
        apps_synced=apps.changes,
        apps_sync_error=apps.error,
        restart_required=apps.restart_required,
    )


def _submit_bulk_deploys(
    body: BulkActionRequest,
    user: Any,
    registry: Any,
    settings: Any,
    request: Request,
) -> BulkActionResponse:
    """Pre-check and submit one ``source:deploy`` task per accepted source."""
    manager = request.app.state.task_manager
    deployments: list[BulkDeployTask] = []
    failed: list[dict[str, str]] = []

    for item in body.sources:
        if not registry.source_exists(item.source):
            failed.append(
                {
                    "source": item.source,
                    "code": "not_found",
                    "error": f"Source '{item.source}' not found",
                }
            )
            continue
        if item.version is not None:
            try:
                source = registry.get_source(item.source)
            except SourceNotFoundError:
                failed.append(
                    {
                        "source": item.source,
                        "code": "not_found",
                        "error": f"Source '{item.source}' not found",
                    }
                )
                continue
            if item.version not in source.versions:
                failed.append(
                    {
                        "source": item.source,
                        "code": "not_found",
                        "error": (f"Version '{item.version}' not found for source '{item.source}'"),
                    }
                )
                continue

        info = manager.submit(
            _SOURCE_DEPLOY_TASK_KIND,
            _run_source_deploy_task,
            item.source,
            item.version,
            body.dry_run,
            user,
            registry,
            settings,
            request,
        )
        deployments.append(
            BulkDeployTask(
                source=item.source,
                version=item.version,
                task_id=info.id,
                kind=info.kind,
            )
        )

    if deployments:
        audit_resource_change(
            user.user_id,
            "schema",
            ",".join(entry.source for entry in deployments),
            "bulk_deploy_submitted",
            details={
                "task_ids": [entry.task_id for entry in deployments],
                "dry_run": body.dry_run,
            },
        )
    return BulkActionResponse(
        action="deploy",
        succeeded=[entry.source for entry in deployments],
        failed=failed,
        deployments=deployments,
    )


def _apply_bulk(
    body: BulkActionRequest,
    action_to_state: dict[str, str],
    user: Any,
    registry: Any,
    settings: Any,
) -> tuple[list[str], list[dict[str, str]], list[Source]]:
    """Apply a bulk action source by source, returning the names done, the failures and the deleted."""
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    deleted: list[Source] = []
    records = SourceDeploymentStore.from_settings(settings)
    for item in body.sources:
        name = item.source
        try:
            if body.action == "delete":
                # Read before the delete, for the topics it owns. A name with no
                # stored source still deletes, as it did before it had topics.
                stored = registry.get_source(name) if registry.source_exists(name) else None
                registry.delete_source(name, created_by=git_author(user), deployment_store=records)
                if stored is not None:
                    deleted.append(stored)
            else:
                source = registry.get_source(name)
                updated = source.model_copy(update={"state": action_to_state[body.action]})
                registry.save_source(updated, created_by=git_author(user))
            succeeded.append(name)
        except Exception as e:
            code = _failure_code(e)
            # A refusal the registry composed is the caller's to read; anything else may be
            # the deploy repo's own text.
            error = str(e)
            if code == "internal_error":
                error = hide_backend_text(
                    "the source could not be written; the engine log has the reason",
                    e,
                    event="bulk source action failed",
                    source=name,
                )
            failed.append({"source": name, "code": code, "error": error})
    return succeeded, failed, deleted


@router.post(
    "/reconcile-apps",
    response_model=AppsReconcileResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def reconcile_apps(user: CurrentUser, registry: SourceReg, request: Request):
    """Bring the deploy repo's derived app state into step with the sources.

    Recompiles every stack-scoped routing block (receiver, loader, archiver) and
    deploys, syncs or removes the instances of every instance-scoped app (a fetcher
    per active fetcher-based source). Every source write does this on its own; this
    route is the retry when one reported ``apps_sync_error``, and the pass that
    deletes a stranded ``_load`` topic a deploy reported in ``topics_pending_removal``.
    """
    if getattr(request.app.state, "gitcrud", None) is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    apps = reconcile_derived_apps(request, user, registry)
    if apps.error:
        raise HTTPException(
            status_code=502,
            detail={"code": "reconcile_failed", "message": apps.error, "changes": apps.changes},
        )
    return AppsReconcileResponse(
        changes=apps.changes,
        restart_required=apps.restart_required,
        topics_removed=apps.topics_removed,
        topics_pending_removal=apps.topics_pending_removal,
    )


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def seed_sources(user: CurrentUser, registry: SourceReg):
    """Seed built-in default source definitions. Non-destructive (skips existing)."""
    count = registry.seed_builtin_sources(overwrite=False)
    audit_resource_change(user.user_id, "source", "all", "seeded")
    return SeedResponse(seeded=count)


@router.post(
    "/import",
    response_model=SourceImportResponse,
    status_code=201,
    responses={
        409: {
            "model": ErrorResponse,
            "description": (
                "The source name already exists here, or a meta schema the bundle carries "
                "would land on an occupied path (code conflict)"
            ),
        },
    },
    dependencies=[Depends(require_action(scopes_dict["source_write"]))],
)
def import_source(
    body: SourceBundle,
    user: CurrentUser,
    registry: SourceReg,
    schema_registry: SchemaReg,
    request: Request,
) -> SourceImportResponse:
    """Import a source bundle exported from another deployment.

    The bundle's meta-schema definitions are written first, then the source, both
    through the registries that commit to git. A definition marked
    ``resource_type: core`` is resolved against what dfe-schemas already put here
    and never written, so the read-only schema is not forked.

    Every check runs before the first write: a bundle applied halfway would leave
    the source's schema pins with no definitions behind them. A schema pin, or a
    version it pins, that neither the bundle nor this deployment defines is a 422
    ``unresolved_reference``.
    """
    try:
        result = apply_source_bundle(
            registry,
            schema_registry,
            body,
            created_by=git_author(user),
        )
    except ExchangeError as exc:
        raise_exchange_http(exc)

    if result.action == "resolved":
        return SourceImportResponse(**result.model_dump())

    audit_resource_change(user.user_id, "source", result.source, "created")
    apps = reconcile_derived_apps(request, user, registry)
    return SourceImportResponse(
        **result.model_dump(),
        apps_synced=apps.changes,
        apps_sync_error=apps.error,
        restart_required=apps.restart_required,
    )


# -- Helpers --


def _source_catalogue() -> source_catalogue_module.SourceCatalogue:
    """The installed catalogue, or a 502 whose detail goes to the engine log, not the caller."""
    try:
        return source_catalogue_module.source_catalogue()
    except ManifestError as exc:
        logger.error("Source catalogue unreadable", error=str(exc))
        raise HTTPException(
            status_code=502,
            detail={
                "code": "catalogue_error",
                "message": "the source catalogue could not be read; the engine log has the reason",
            },
        ) from exc


def _to_catalogue_object(*, entry: source_catalogue_module.CatalogueEntry) -> CatalogueEntryObject:
    """One catalogue entry as the console lists it, judged against the deployed transform."""
    transform = entry.request.transform
    available = source_catalogue_module.variant_availability(transform=transform)
    return CatalogueEntryObject(
        availability=available.state,
        availability_reason=available.reason,
        beats=dict(entry.beats),
        data_stream=entry.data_stream,
        dataset=entry.dataset,
        default_intake=entry.default_intake,
        description=entry.request.description,
        display_name=entry.request.display_name,
        engine=transform.engine if transform else None,
        framing=entry.framing,
        has_schema=entry.request.schema_config is not None,
        intakes=sorted(entry.intakes),
        name=entry.name,
        package=entry.package,
        variant=transform.variant if transform else None,
        variants=list(entry.variants),
    )


def _resolve_source_version(
    registry: SourceReg,
    name: str,
    version: str | None,
    *,
    default_current: bool = False,
) -> tuple[Source, str]:
    """Resolve source and version id for schema operations."""
    try:
        source = registry.get_source(name)
    except SourceNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Source '{name}' not found"},
        ) from None

    if version:
        version_id = version
    elif default_current:
        version_id = source.current
    else:
        version_id = source.runtime_version_id()

    if version_id not in source.versions:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Version '{version_id}' not found for source '{name}'",
            },
        )
    return source, version_id


def _version_detail_from_snapshot(
    source_name: str,
    version_id: str,
    snap: SourceVersion,
    store: SourceDeploymentStore,
) -> SourceVersionDetail:
    build = store.load_build(source_name, version_id)
    deploy = store.load_deploy(source_name, version_id)
    return SourceVersionDetail(
        **snap.model_dump(mode="json"),
        source_build=_build_to_response(build) if build else None,
        source_deployment=deploy,
    )


def _build_to_response(artifact: SourceBuildArtifact) -> SchemaBuildResult:
    views = dict(artifact.view_ddls)
    ddl = None
    if artifact.create_table_ddl:
        ddl = DDLResult(
            source_name=artifact.source_name,
            create_table=artifact.create_table_ddl,
            views=views,
        )
    return SchemaBuildResult(
        source_name=artifact.source_name,
        version=artifact.version,
        columns=[],
        ddl=ddl,
        validation_errors=list(artifact.validation_errors),
        built_at=artifact.built_at,
        column_count=artifact.column_count,
    )


def _to_source_detail_response(
    source: Source,
    store: SourceDeploymentStore,
) -> SourceDetailResponse:
    versions: dict[str, SourceVersionDetail] = {}
    for version_id, snap in source.versions.items():
        versions[version_id] = _version_detail_from_snapshot(source.source, version_id, snap, store)
    return SourceDetailResponse(
        **source.model_dump(mode="json", exclude={"versions"}),
        versions=versions,
    )


def _plan_to_response(plan: SourcePlanArtifact) -> SourcePlanResponse:
    ddl = None
    if plan.create_table_ddl:
        ddl = DDLResult(
            source_name=plan.source_name,
            create_table=plan.create_table_ddl,
            views=dict(plan.view_ddls),
        )
    return SourcePlanResponse(
        source_name=plan.source_name,
        version=plan.version,
        planned_at=plan.planned_at,
        table_exists=plan.table_exists,
        validation_errors=list(plan.validation_errors),
        statements=list(plan.statements),
        ddl=ddl,
        ready=plan.ready,
        ready_reason=plan.ready_reason,
        ttl_change=plan.ttl_change,
    )


def _pin_landing_source_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the landing source (``main``) first; other rows keep their order."""
    landing = [row for row in rows if row.get("source") == DEFAULT_LANDING_LABEL]
    rest = [row for row in rows if row.get("source") != DEFAULT_LANDING_LABEL]
    return landing + rest


def _to_summary(raw: dict[str, Any]) -> SourceSummaryObject:
    """Convert registry list row to ``SourceSummaryObject``."""
    return SourceSummaryObject(
        name=raw.get("source", ""),
        resource_type=raw.get("resource_type", "custom"),
        display_name=raw.get("display_name"),
        description=raw.get("description"),
        state=raw.get("state", "active"),
        enabled=raw.get("enabled", True),
        current=raw.get("current", "1.0.0"),
        deployed_version=raw.get("deployed_version"),
        versions=list(raw.get("versions") or []),
        updated_at=raw.get("updated_at") or "",
        header_type=raw.get("header_type"),
        has_transform=bool(raw.get("has_transform")),
        has_fetcher=bool(raw.get("has_fetcher")),
        origin=raw.get("origin"),
        current_table_topic_type=raw.get("current_table_topic_type", "main"),
        views=list(raw.get("views") or []),
    )


def _normalize_attribute(value: Any) -> list[str] | None:
    """Match meta-schema column ``attribute``: list[str] | None."""
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _to_api_schema_column(col: Any) -> SchemaColumn:
    return SchemaColumn(
        name=col.name,
        type=col.type,
        use_case=getattr(col, "use_case", "") or "",
        attribute=_normalize_attribute(getattr(col, "attribute", None)),
        description=getattr(col, "comment", None) or getattr(col, "description", "") or "",
    )
