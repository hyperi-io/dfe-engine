#  Project:      dfe-engine
#  File:         api/v1/ingest.py
#  Purpose:      The receiver's ingest rules: block order and compatibility profiles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The ingest rule set's order and compatibility profiles.

GET  /api/v1/ingest/rules                       -> every rule block, in the order it runs
POST /api/v1/ingest/rules/order                 -> reorder one field's blocks
GET  /api/v1/ingest/compat                      -> the deployment's profile bindings
PUT  /api/v1/ingest/compat                      -> apply them
GET  /api/v1/ingest/compat-profiles             -> list the stored profiles
GET  /api/v1/ingest/compat-profiles/{profile}   -> get one

The receiver's rules are compiled from the sources and the profiles they bind,
one block per source and per bound profile, in the order the deployment stores.
A reorder sends the whole list of one field's blocks with the ``If-Match`` the
list was read with. A list naming other blocks than the ones there now answers
409 with the current list, a pinned block 422, and a stale ``If-Match`` 412.
Both writes take ``dry_run`` and end by bringing the apps into step, and
``reload`` says whether the change reached them without a restart.

Reads need ``source:read`` at system scope or in one of the caller's orgs, writes
``source:write``. The rule blocks of a fetcher source bound to an org the caller does
not hold are left out of the list. The bindings and profiles are the deployment's
own and shared by every source, so any reader sees them.
"""

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from scalo.api import (
    DryRun,
    FieldError,
    ListParams,
    Page,
    PreconditionHeaders,
    ProblemError,
    paginate,
)

from dfe_engine.api.deps import CurrentUser, SourceReg, require_action, require_own_org_action
from dfe_engine.api.errors import ErrorCode, ErrorResponse
from dfe_engine.api.v1.sources import (
    AppsSync,
    held_source_orgs,
    reconcile_derived_apps,
    source_model_is_readable,
)
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import ConcurrencyConflictError
from dfe_engine.ingest.models import (
    CompatCover,
    CompatProfile,
    CompatSetting,
    HeaderField,
    IngestRules,
    RuleOrder,
)
from dfe_engine.ingest.store import (
    CompatProfileNotFoundError,
    IngestStore,
    compat_etag,
    order_etag,
    profile_etag,
)
from dfe_engine.services.source_routing import (
    BlockKind,
    CompiledRules,
    RuleBlock,
    compile_ingest_rules,
    effective_bindings,
)
from dfe_engine.source.models import CompatBinding
from dfe_engine.source.registry import (
    RuleProblem,
    binding_problems,
    bound_versions,
    rule_id_problems,
    version_problems,
)

router = APIRouter(prefix="/ingest", tags=["Ingest rules"], dependencies=[WRITE_TURN])

_READ = Depends(require_own_org_action(scopes_dict["source_read"]))
_WRITE = Depends(require_action(scopes_dict["source_write"]))

RuleTarget = Literal["_source", "_org_id"]

# A write retried this many times over unrelated commits before it answers 409.
_WRITE_ATTEMPTS = 3

_REFUSED: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ErrorResponse,
        "description": (
            "Refused, nothing written: a block named twice or a pinned block (code "
            "invalid_order), or a binding the receiver could not compile (code invalid_binding)"
        ),
    },
    503: {"model": ErrorResponse, "description": "No deploy repo, where the ingest rules live"},
}


# -- Models ------------------------------------------------------------------


class RuleBlockResponse(BaseModel):
    """One block of the receiver's rule set: a source's rows, a profile's, or the default."""

    id: str = Field(description="source.<name>, compat.<profile> or default")
    target: RuleTarget = Field(description="The header field the block's rows set")
    position: int = Field(description="Where it runs among the field's blocks, from 1")
    kind: BlockKind = Field(description="source, profile, or the receiver's own default")
    name: str = Field(description="The source's name or the profile's id, empty for the default")
    version: str | None = Field(default=None, description="The profile version compiled")
    pinned: bool = Field(description="Runs last whatever the order says, and refuses a move")
    compiled: bool = Field(
        description="Whether its rows are in the receiver's rule set. False where they do not compile"
    )
    rules: list[dict[str, Any]] = Field(
        description="The rows as the receiver reads them, in the order they fire. Empty for the default"
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="What is wrong with it: a row that does not compile, or one an earlier row shadows",
    )


class RuleOrderRequest(BaseModel):
    """One field's blocks, every one of them, in the order they are to run."""

    model_config = ConfigDict(extra="forbid")

    target: RuleTarget = Field(description="The header field whose blocks are reordered")
    blocks: list[str] = Field(
        description="Every block id GET /ingest/rules lists for target bar the pinned default"
    )


class _AppsFields(BaseModel):
    dry_run: bool = Field(description="Nothing was written: this is what the write would do")
    reload: Literal["hot", "restart"] = Field(
        description=(
            "hot where every app took the change where it stands, restart where "
            "restart_required names a command"
        )
    )
    apps_synced: list[str] = Field(
        default_factory=list, description="Deploy-repo writes made so the apps follow the change"
    )
    apps_sync_error: str | None = Field(
        default=None, description="Why the apps were not brought into step, if not"
    )
    restart_required: list[str] = Field(
        default_factory=list, description="One command per app that cannot take the change in place"
    )


class RuleOrderResponse(_AppsFields):
    """The field's blocks as the reorder leaves them."""

    target: RuleTarget
    blocks: list[RuleBlockResponse]


class CompatResponse(BaseModel):
    """The profiles every source binds unless it names its own list."""

    profiles: list[CompatBinding] = Field(description="The deployment's default bindings")


class CompatWriteResponse(CompatResponse, _AppsFields):
    """The bindings an apply leaves, and what the apps did with them."""


class CompatProfileSummary(BaseModel):
    """A stored profile at its current version, as a list shows it."""

    id: str
    resource_type: Literal["core", "custom"]
    current: str = Field(description="The version a binding takes when it names none")
    versions: list[str] = Field(description="Every published version")
    title: str
    description: str = Field(description="What it reads and what it sets")
    covers: list[CompatCover] = Field(description="The senders it reads")
    targets: list[HeaderField] = Field(description="The header fields its rows set")
    opt_in: list[str] = Field(description="Rows left out until a binding opts in to them")


# -- Helpers -----------------------------------------------------------------


def _store(registry: Any) -> IngestStore:
    """Raises: HTTPException: 503 ``not_configured`` without a deploy repo."""
    store = registry.ingest_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "not_configured",
                "message": "The ingest rules live in the deploy repo, and gitops is not enabled",
            },
        )
    return store


def _compiled(registry: Any, ingest: IngestRules) -> CompiledRules:
    return compile_ingest_rules(registry.get_all_sources(states=("active",)), ingest)


def _block(block: RuleBlock, position: int, compiled: CompiledRules) -> RuleBlockResponse:
    return RuleBlockResponse.model_validate(
        {
            "id": block.id,
            "target": block.target,
            "position": position,
            "kind": block.kind,
            "name": block.name,
            "version": block.version,
            "pinned": block.pinned,
            "compiled": block.id not in compiled.dropped,
            "rules": [
                rule.model_dump(mode="json", by_alias=True, exclude_defaults=True)
                for rule in block.rules
            ],
            "warnings": list(compiled.warnings.get(block.id, ())),
        }
    )


def _blocks(compiled: CompiledRules, target: str) -> list[RuleBlockResponse]:
    return [
        _block(block, position, compiled)
        for position, block in enumerate(compiled.blocks[target], start=1)
    ]


def _movable(compiled: CompiledRules, target: str) -> list[str]:
    return [block.id for block in compiled.blocks[target] if not block.pinned]


def _apps_fields(apps: AppsSync | None, *, dry_run: bool) -> dict[str, Any]:
    synced = apps or AppsSync(changes=[])
    return {
        "dry_run": dry_run,
        "reload": "restart" if synced.restart_required else "hot",
        "apps_synced": synced.changes,
        "apps_sync_error": synced.error,
        "restart_required": synced.restart_required,
    }


def _refuse(code: str, message: str, problems: list[RuleProblem]) -> ProblemError:
    return ProblemError(
        422,
        message,
        code=code,
        errors=[
            FieldError(field=problem.field, location="body", detail=problem.detail, code=code)
            for problem in problems
        ],
    )


def _conditional_write[T](
    write: Callable[[str | None], object],
    current: Callable[[dict[str, Any]], T],
    etag: Callable[[T], str],
    wanted: str,
    head: str | None,
    what: str,
) -> None:
    """Write against *head*, and against each newer head while the resource there is still *wanted*.

    A refused push is never re-applied blind: the stored document at the newer
    head is read back and its ETag compared with the one the request's
    ``If-Match`` was checked against, so another write to it is never overwritten.

    Raises:
        ProblemError: 412 ``etag_mismatch`` when another write changed it, 409
            ``conflict`` when the deploy repo kept moving over every attempt.
    """
    base = head
    for _ in range(_WRITE_ATTEMPTS):
        try:
            write(base)
            return
        except ConcurrencyConflictError as exc:
            try:
                now = etag(current(exc.current or {}))
            except ValidationError:
                now = ""
            if now != wanted:
                raise ProblemError(
                    412,
                    f"The {what} changed while this request was writing it",
                    code="etag_mismatch",
                    etag=now or None,
                ) from exc
            base = exc.head
    raise ProblemError(
        409,
        f"The deploy repo kept moving while this request wrote the {what}, retry",
        code=ErrorCode.CONFLICT,
    )


# -- Rules -------------------------------------------------------------------


def _readable_blocks(
    request: Request, user: CurrentUser, registry: SourceReg, blocks: list[RuleBlockResponse]
) -> list[RuleBlockResponse]:
    """The blocks ``user`` may read: a source's block only where the caller may read the source."""
    held = held_source_orgs(request, user)
    if held is None:
        return blocks
    readable = {
        source.source
        for source in registry.get_all_sources()
        if source_model_is_readable(source, held)
    }
    return [block for block in blocks if block.kind != "source" or block.name in readable]


@router.get("/rules", response_model=Page[RuleBlockResponse], dependencies=[_READ])
def list_rule_blocks(
    request: Request, user: CurrentUser, registry: SourceReg, response: Response, query: ListParams
) -> Page[RuleBlockResponse]:
    """List every rule block, ``_source`` first, each field's in the order it runs.

    Its ``ETag`` is the ``If-Match`` a reorder takes. Takes no filter or order_by.
    """
    if query.filter or query.order_by:
        raise ProblemError(
            400,
            "This list is in the order the rules run and takes no filter",
            code="unsupported_query",
        )
    store = _store(registry)
    ingest = store.read()
    compiled = _compiled(registry, ingest)
    response.headers["ETag"] = order_etag(ingest.order)
    blocks = [*_blocks(compiled, "_source"), *_blocks(compiled, "_org_id")]
    return paginate(_readable_blocks(request, user, registry, blocks), query)


@router.post(
    "/rules/order",
    response_model=RuleOrderResponse,
    responses={
        409: {
            "model": ErrorResponse,
            "description": (
                "The list names other blocks than the field has now, because a source or a "
                "binding changed since it was read (code order_conflict, the current list in "
                "context.blocks), or the deploy repo kept moving (code conflict)"
            ),
        },
        **_REFUSED,
    },
    dependencies=[_WRITE],
)
def reorder_rule_blocks(
    body: RuleOrderRequest,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
    response: Response,
    preconditions: PreconditionHeaders,
    dry_run: DryRun,
) -> RuleOrderResponse:
    """Set the order one field's rule blocks run in.

    ``If-Match`` is required, and a stale one answers 412 with the current ETag.
    The pinned ``default`` keeps its place last. A rules-only change is one the
    receiver reloads where it stands, so ``reload`` answers ``hot``.
    """
    store = _store(registry)
    order, head = store.order_at_head()
    current_etag = order_etag(order)
    preconditions.check(current_etag)

    named = body.blocks
    problems = [
        RuleProblem(f"blocks.{index}", f"{block_id!r} is listed twice")
        for index, block_id in enumerate(named)
        if named.index(block_id) != index
    ]
    profiles = store.profiles()
    defaults = tuple(store.compat().profiles)
    compiled = _compiled(registry, IngestRules(order, profiles, defaults))
    pinned = {block.id for block in compiled.blocks[body.target] if block.pinned}
    problems += [
        RuleProblem(f"blocks.{index}", f"{block_id!r} is pinned and keeps its place")
        for index, block_id in enumerate(named)
        if block_id in pinned
    ]
    if problems:
        raise _refuse("invalid_order", "The order is not one the rule set can take", problems)
    movable = _movable(compiled, body.target)
    if sorted(named) != sorted(movable):
        raise ProblemError(
            409,
            f"The {body.target} blocks are not the ones listed: send every one of them",
            code="order_conflict",
            target=body.target,
            blocks=movable,
        )

    desired = order.with_ids(body.target, named)
    response.headers["ETag"] = order_etag(desired)
    reordered = _compiled(registry, IngestRules(desired, profiles, defaults))
    if dry_run:
        return RuleOrderResponse(
            target=body.target,
            blocks=_blocks(reordered, body.target),
            **_apps_fields(None, dry_run=True),
        )
    _conditional_write(
        lambda base: store.put_order(desired, user.user_id, base_revision=base),
        RuleOrder.model_validate,
        order_etag,
        current_etag,
        head,
        "rule order",
    )
    audit_resource_change(user.user_id, "ingest_rules", body.target, "reordered", {"blocks": named})
    apps = reconcile_derived_apps(request, user, registry)
    return RuleOrderResponse(
        target=body.target,
        blocks=_blocks(reordered, body.target),
        **_apps_fields(apps, dry_run=False),
    )


# -- Compatibility -----------------------------------------------------------


def _binding_refusals(
    setting: CompatSetting, registry: Any, ingest: IngestRules
) -> list[RuleProblem]:
    """What would stop the deployment's bindings compiling for the sources that take them."""
    problems = binding_problems(setting.profiles, ingest.profiles, field="profiles")
    mine = bound_versions(setting.profiles, ingest.profiles)
    theirs: list[tuple[str, dict[str, str]]] = []
    for source in registry.get_all_sources(states=("active", "dormant")):
        if source.compat_profiles is None:
            problems += rule_id_problems(
                setting.profiles, ingest.profiles, source.source, field="profiles"
            )
        else:
            theirs.append(
                (source.source, bound_versions(effective_bindings(source, ingest), ingest.profiles))
            )
    return problems + version_problems(mine, theirs, field="profiles")


@router.get("/compat", response_model=CompatResponse, dependencies=[_READ])
def get_compat(registry: SourceReg, response: Response) -> CompatResponse:
    """Get the profiles every source binds unless it names its own list. Its ``ETag`` is an apply's ``If-Match``."""
    setting = _store(registry).compat()
    response.headers["ETag"] = compat_etag(setting)
    return CompatResponse(profiles=setting.profiles)


@router.put(
    "/compat", response_model=CompatWriteResponse, responses=_REFUSED, dependencies=[_WRITE]
)
def apply_compat(
    body: CompatSetting,
    user: CurrentUser,
    registry: SourceReg,
    request: Request,
    response: Response,
    preconditions: PreconditionHeaders,
    dry_run: DryRun,
) -> CompatWriteResponse:
    """Set the profiles every source binds unless it names its own list.

    ``If-Match`` is required. Each binding must name a stored profile, a version it
    publishes and only opt-in rows it offers, and one version of a profile compiles
    per deployment, so a version another source binds differently is refused with
    422 ``invalid_binding``. A rules-only change answers ``reload: hot``.
    """
    store = _store(registry)
    setting, head = store.compat_at_head()
    current_etag = compat_etag(setting)
    preconditions.check(current_etag)
    ingest = IngestRules(store.order(), store.profiles(), tuple(body.profiles))
    problems = _binding_refusals(body, registry, ingest)
    if problems:
        raise _refuse(
            "invalid_binding", "The bindings are not ones the receiver can compile", problems
        )

    response.headers["ETag"] = compat_etag(body)
    if dry_run:
        return CompatWriteResponse(profiles=body.profiles, **_apps_fields(None, dry_run=True))
    _conditional_write(
        lambda base: store.put_compat(body, user.user_id, base_revision=base),
        CompatSetting.model_validate,
        compat_etag,
        current_etag,
        head,
        "compatibility bindings",
    )
    audit_resource_change(
        user.user_id,
        "ingest_compat",
        "deployment",
        "updated",
        {"profiles": [binding.profile for binding in body.profiles]},
    )
    apps = reconcile_derived_apps(request, user, registry)
    return CompatWriteResponse(profiles=body.profiles, **_apps_fields(apps, dry_run=False))


@router.get("/compat-profiles", response_model=Page[CompatProfileSummary], dependencies=[_READ])
def list_compat_profiles(registry: SourceReg, query: ListParams) -> Page[CompatProfileSummary]:
    """List the stored compatibility profiles by id, each at its current version. Takes no filter or order_by."""
    if query.filter or query.order_by:
        raise ProblemError(
            400, "This list is ordered by id and takes no filter", code="unsupported_query"
        )
    rows = []
    for profile_id, profile in sorted(_store(registry).profiles().items()):
        entry = profile.versions[profile.current]
        rows.append(
            CompatProfileSummary(
                id=profile_id,
                resource_type=profile.resource_type,
                current=profile.current,
                versions=sorted(profile.versions),
                title=entry.title,
                description=entry.description,
                covers=entry.covers,
                targets=entry.targets,
                opt_in=entry.opt_in,
            )
        )
    return paginate(rows, query)


@router.get(
    "/compat-profiles/{profile}",
    response_model=CompatProfile,
    responses={404: {"model": ErrorResponse, "description": "No such profile is stored"}},
    dependencies=[_READ],
)
def get_compat_profile(profile: str, registry: SourceReg, response: Response) -> CompatProfile:
    """Get one compatibility profile with every version it publishes."""
    try:
        held = _store(registry).profile(profile)
    except CompatProfileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Compatibility profile {profile!r} not found"},
        ) from None
    response.headers["ETag"] = profile_etag(held)
    return held
