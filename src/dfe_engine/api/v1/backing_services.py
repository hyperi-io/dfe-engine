#  Project:      dfe-engine
#  File:         api/v1/backing_services.py
#  Purpose:      Read the backing services' declared deploy config; govern the
#                overlay that declares it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deploy repo's substrate overlay, read and governed.

GET    /api/v1/backing-services                        -> declared config per service
GET    /api/v1/backing-services/{service}              -> one service
GET    /api/v1/backing-services/overlays               -> list overlay resources
GET    /api/v1/backing-services/overlays/{name}/vars   -> flattened vars (+ protected)
PUT    /api/v1/backing-services/overlays/{name}/vars/{path}    -> set a var
DELETE /api/v1/backing-services/overlays/{name}/vars/{path}    -> revert a var

The overlay files are the `infravars` class - the deploy repo's `infra/` directory,
which the platform and data ApplicationSets layer last. They are the charts that
are NOT app instances, so they cannot live in `values/`: the app appset's
`values/*-values.yaml` glob reaches git as a pathspec, where `*` matches `/`, and
any file it matches becomes an Argo application.

Reads are DECLARED values, never observed ones. The engine has no Kubernetes
client, so nothing here reports a pod phase, a running replica count or actual
disk use - only what the deploy repo says the deployment asked for.

Node and broker counts are UP-ONLY here, and not out of caution: both stores place
data per member, so removing one takes its copy with it unless something moves the
data off first. CPU and memory move freely both ways.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeGuard

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud.engine import ResourceNotFoundError, get_path, set_path

from .helm import (
    SetVarRequest,
    WriteResult,
    check_name,
    delete_var_governed,
    gitcrud_of,
    policy_of,
    set_var_governed,
)

router = APIRouter(prefix="/backing-services", tags=["Governed Ops: Backing Services"])

_CLASS = "infravars"

# The deployment-wide overlay, read before the per-chart one so the per-chart file
# wins - the same order the ApplicationSets stack them in.
_COMMON = "common"


class BackingReload(StrEnum):
    """What Argo syncing this change actually does to the running store."""

    APPLY = "apply"
    """The operator reconciles it in place; nothing restarts."""

    ROLL = "roll"
    """The pod template or server config changed, so the operator restarts pods."""

    RECREATE = "recreate"
    """Needs the StatefulSet recreated by hand - volumeClaimTemplates are immutable."""

    REDEPLOY = "redeploy"
    """The Application's object set changes shape; the store may be deployed or removed."""


@dataclass(frozen=True)
class BackingService:
    """One backing service: where its values live and what they are called.

    A table rather than code so adding a service is an edit, not a redesign.
    """

    service: str
    chart: str
    prefix: str
    # Value paths under `prefix`, in the order the API reports them.
    replicas: str
    storage_size: str
    storage_class: str
    scale_down_reason: str
    """Why lowering a member count here loses data. Returned verbatim on refusal."""

    extra_member_counts: tuple[str, ...] = field(default_factory=tuple)
    """Further member counts under `prefix` that are up-only for the same reason."""


CATALOGUE: tuple[BackingService, ...] = (
    BackingService(
        service="clickhouse",
        chart="clickhouse-cluster",
        prefix="clickhouse",
        replicas="replicas",
        storage_size="storage.size",
        storage_class="storage.storageClass",
        scale_down_reason=(
            "removing a ClickHouse node drops a copy of the data, or the data itself "
            "when the cluster is sharded. Move or re-replicate it first, then lower "
            "the count in the deploy repo"
        ),
        # A Keeper ensemble is a Raft quorum: shrinking it can lose the quorum
        # outright, which takes every ReplicatedMergeTree table read-only with it.
        extra_member_counts=("keeper.replicas",),
    ),
    BackingService(
        service="kafka",
        chart="kafka",
        prefix="kafka",
        replicas="replicas",
        storage_size="storage.size",
        storage_class="storage.storageClass",
        scale_down_reason=(
            "every partition on a Kafka broker must be reassigned off it before the "
            "broker goes, or the replicas it held go with it. Reassign first, then "
            "lower the count in the deploy repo"
        ),
    ),
)

_BY_SERVICE = {b.service: b for b in CATALOGUE}

_RESOURCE_PATHS = (
    "resources.requests.cpu",
    "resources.requests.memory",
    "resources.limits.cpu",
    "resources.limits.memory",
)

# What a written key does once Argo has it, keyed by the first segment below the
# service prefix. Matched by segment equality, not string prefix, so `storage` and
# `storageModel` stay distinct. Anything unlisted falls to APPLY.
_RELOAD_BY_SEGMENT: dict[str, BackingReload] = {
    # mode=external removes the store's objects; mode=cluster creates them.
    "mode": BackingReload.REDEPLOY,
    # Server or broker config -- the operator restarts pods onto the new settings.
    "storageModel": BackingReload.ROLL,
    "s3": BackingReload.ROLL,
    "tiered": BackingReload.ROLL,
    "resources": BackingReload.ROLL,
    # volumeClaimTemplates are immutable, so no sync can apply a size or class change.
    "storage": BackingReload.RECREATE,
}


class DeclaredValue(BaseModel):
    """One value as the deploy repo declares it.

    ``source`` names the overlay file it came from; a null source means the
    deployment declared nothing and the chart or profile default applies. Those
    defaults live in dfe-infra, which the engine does not read, so the API says
    "not declared" rather than guessing a number.
    """

    value: Any | None = None
    source: str | None = None
    protected: bool = False


class BackingServiceConfig(BaseModel):
    """A backing service's deploy configuration AS DECLARED, not as observed."""

    service: str
    chart: str
    overlay: str
    prefix: str = Field(
        description=(
            "Values-key prefix every path below sits under. Read it rather than "
            "deriving one from `service` or `chart` - neither is guaranteed to match."
        )
    )
    mode: DeclaredValue
    storage_model: DeclaredValue
    replicas: DeclaredValue
    storage_size: DeclaredValue
    storage_class: DeclaredValue
    resources: dict[str, DeclaredValue] = Field(default_factory=dict)


class BackingWriteResult(WriteResult):
    """A governed write plus what syncing it does to the running store."""

    reload: str = Field(
        description=(
            "'apply' reconciles in place, 'roll' restarts pods, 'recreate' needs the "
            "StatefulSet recreated by hand, 'redeploy' changes which objects exist."
        )
    )


def _docs(request: Request, chart: str) -> list[tuple[str, dict]]:
    """The overlay documents that apply to one chart, lowest precedence first."""
    gc = gitcrud_of(request)
    out: list[tuple[str, dict]] = []
    for name in (_COMMON, chart):
        try:
            out.append((name, gc.get(_CLASS, name)))
        except ResourceNotFoundError:
            continue
    return out


def _declared(
    request: Request, docs: list[tuple[str, dict]], chart: str, path: str
) -> DeclaredValue:
    """Resolve one dot-path across the overlay stack, last declaration winning."""
    policy = policy_of(request)
    found = DeclaredValue(
        protected=bool(policy and policy.is_protected(_CLASS, chart, path)),
    )
    for name, doc in docs:
        value = get_path(doc, path, default=None)
        if value is not None:
            found = DeclaredValue(value=value, source=name, protected=found.protected)
    return found


def _config(request: Request, spec: BackingService) -> BackingServiceConfig:
    docs = _docs(request, spec.chart)

    def at(leaf: str) -> DeclaredValue:
        return _declared(request, docs, spec.chart, f"{spec.prefix}.{leaf}")

    return BackingServiceConfig(
        service=spec.service,
        chart=spec.chart,
        overlay=f"{spec.chart}.yaml",
        prefix=spec.prefix,
        mode=at("mode"),
        storage_model=at("storageModel"),
        replicas=at(spec.replicas),
        storage_size=at(spec.storage_size),
        storage_class=at(spec.storage_class),
        resources={p: at(p) for p in _RESOURCE_PATHS},
    )


def _owner(path: str) -> BackingService | None:
    """The backing service whose values prefix owns this dot-path."""
    head = path.split(".", 1)[0]
    for spec in CATALOGUE:
        if spec.prefix == head:
            return spec
    return None


def _reload_for(path: str) -> BackingReload:
    """What syncing a write to this path does to the running store."""
    spec = _owner(path)
    if spec is None:
        return BackingReload.APPLY
    segments = path.split(".")
    if len(segments) < 2:
        return BackingReload.APPLY
    return _RELOAD_BY_SEGMENT.get(segments[1], BackingReload.APPLY)


def _resolved(docs: list[tuple[str, dict]], path: str) -> Any | None:
    """The value the overlay stack declares for a path, last declaration winning."""
    found: Any | None = None
    for _name, doc in docs:
        value = get_path(doc, path, default=None)
        if value is not None:
            found = value
    return found


def _is_count(value: Any) -> TypeGuard[int]:
    """A member count is a plain int; bool is an int in Python and is not one."""
    return isinstance(value, int) and not isinstance(value, bool)


def _stack_with(
    request: Request, spec: BackingService, name: str, path: str, value: Any
) -> list[tuple[str, dict]]:
    """The overlay stack as it would read with this write applied to ``name``."""
    gc = gitcrud_of(request)
    out: list[tuple[str, dict]] = []
    for candidate in (_COMMON, spec.chart):
        try:
            doc = copy.deepcopy(gc.get(_CLASS, candidate))
        except ResourceNotFoundError:
            if candidate != name:
                continue
            doc = {}
        if candidate == name:
            set_path(doc, path, value)
        out.append((candidate, doc))
    return out


def _guard_member_count(request: Request, name: str, path: str, value: Any) -> None:
    """Refuse a write that lowers a declared node or broker count.

    Compares the value the overlay stack declares BEFORE the write with what it
    would declare after, so writing into the shared file cannot be refused for a
    drop the per-chart file goes on to override anyway. Nothing declared means
    nothing to compare against, and the write is accepted.
    """
    spec = _owner(path)
    if spec is None:
        return
    counts = {f"{spec.prefix}.{leaf}" for leaf in (spec.replicas, *spec.extra_member_counts)}
    if path not in counts or name not in (_COMMON, spec.chart):
        return

    before = _resolved(_docs(request, spec.chart), path)
    if not _is_count(before):
        return
    after = _resolved(_stack_with(request, spec, name, path, value), path)
    if _is_count(after) and after < before:
        raise HTTPException(
            400,
            detail={
                "code": "scale_down_refused",
                "message": (
                    f"{path} is up-only: {before} -> {after} would remove a member. "
                    f"{spec.scale_down_reason}."
                ),
            },
        )


@router.get("", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
async def list_backing_services(user: CurrentUser, request: Request) -> list[BackingServiceConfig]:
    """Every backing service's DECLARED deploy configuration.

    Declared, not observed: these are the values the deploy repo asks for, read
    from git. The engine runs no Kubernetes client, so a figure here can differ
    from the cluster whenever Argo has not synced the declaration yet.
    """
    return [_config(request, spec) for spec in CATALOGUE]


@router.get("/overlays", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
async def list_overlays(user: CurrentUser, request: Request) -> list[str]:
    """List substrate/platform overlay resources in the deploy repo's infra/ dir."""
    return gitcrud_of(request).list(_CLASS)


@router.get(
    "/overlays/{name}/vars", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))]
)
async def list_overlay_vars(name: str, user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    """Flattened dot-path vars for one overlay, each marked protected or not."""
    check_name(name)
    gc = gitcrud_of(request)
    policy = policy_of(request)
    return [
        {
            "path": path,
            "value": value,
            "protected": bool(policy and policy.is_protected(_CLASS, name, path)),
        }
        for path, value in gc.vars(_CLASS, name).items()
    ]


@router.get("/{service}", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
async def get_backing_service(
    service: str, user: CurrentUser, request: Request
) -> BackingServiceConfig:
    """One backing service's DECLARED deploy configuration (see the list route)."""
    spec = _BY_SERVICE.get(service)
    if spec is None:
        raise HTTPException(
            404,
            detail={
                "code": "not_found",
                "message": f"unknown backing service {service!r}; known: {sorted(_BY_SERVICE)}",
            },
        )
    return _config(request, spec)


@router.put(
    "/overlays/{name}/vars/{path}",
    response_model=BackingWriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def set_overlay_var(
    name: str,
    path: str,
    body: SetVarRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> BackingWriteResult:
    """Set a substrate/platform value.

    403 with the policy that blocked it when the var is protected - the storage
    model, the data-layer modes and the disk size are decided at deploy, and moving
    one on a live deployment is a data migration. 400 when the value would lower a
    declared node or broker count, which loses data rather than capacity.
    """
    check_name(name)
    _guard_member_count(request, name, path, body.value)
    result = set_var_governed(_CLASS, name, path, body.value, user, request, if_match)
    return BackingWriteResult(**result.model_dump(), reload=str(_reload_for(path)))


@router.delete(
    "/overlays/{name}/vars/{path}",
    response_model=BackingWriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def delete_overlay_var(
    name: str, path: str, user: CurrentUser, request: Request
) -> BackingWriteResult:
    """Revert a substrate/platform value to its chart default. Protected vars refuse.

    Not guarded up-only: reverting a count hands it back to the chart or profile
    default, which the engine cannot read, so there is no after-value to compare.
    """
    result = delete_var_governed(_CLASS, name, path, user, request)
    return BackingWriteResult(**result.model_dump(), reload=str(_reload_for(path)))
