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
disk use - only what the deploy repo says the deployment asked for. The one
exception is a service DFE does not run: its sizing is the provider's or its
operator's, so the read adds what the broker or server itself reports.

Who runs each service comes from the deployer (``deployment.backing``) and the
provider catalogue (``dfe_engine.backing.providers``). Where DFE does not run it,
a write to a value nothing in the deployment reads is refused with 409
``managed_by_provider`` before any other check, and helmvars:override does not
lift it: the commit would land and change nothing.

Node and broker counts are UP-ONLY here, and not out of caution: both stores place
data per member, so removing one takes its copy with it unless something moves the
data off first. CPU and memory move freely both ways. A revert that would lower or
un-declare a count needs helmvars:override, because the chart default it falls back
to is not readable here.
"""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeGuard

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, get_interactive_clickhouse_client, require_action
from dfe_engine.api.errors import ErrorResponse
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.backing import observed, providers
from dfe_engine.backing.providers import ManagedBy, Resolution, SetIn
from dfe_engine.gitcrud.engine import ResourceNotFoundError, del_path, get_path, set_path
from dfe_engine.settings import BackingFactSettings
from dfe_engine.yaml_utils import yaml_load

from .helm import (
    SetVarRequest,
    WriteResult,
    check_name,
    delete_var_governed,
    enforce_protected,
    gitcrud_of,
    has_override,
    policy_of,
    set_var_governed,
    shown_vars,
)

router = APIRouter(
    prefix="/backing-services",
    tags=["Governed Ops: Backing Services"],
    dependencies=[WRITE_TURN],
)

_CLASS = "infravars"

# The deployment-wide overlay, read before the per-chart one so the per-chart file
# wins - the same order the ApplicationSets stack them in.
_COMMON = "common"

# The resolver's data-chart sizing, read under both overlays as layer2-data stacks it.
_SIZING_DIR = "sizing"
_SIZING_SUFFIX = ".data-charts.values.yaml"

MANAGED_REFUSAL = "managed_by_provider"
"""The 409 code for a write to a value DFE does not set because it does not run the service."""


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

    reload_by_segment: Mapping[str, BackingReload] = field(default_factory=dict)
    """Per-service reload verdicts, checked before `_RELOAD_SHARED`.

    The same segment means different things to different operators -- a storage
    model Strimzi reconciles onto a running CR is a disk the ClickHouse operator
    will not add to a live cluster -- so the verdict cannot be keyed on the
    segment alone.
    """

    observe: Callable[[Any], ObservedSizing] | None = None
    """Reads the service's size off the data plane, given the engine settings."""


def _observe_kafka(settings: Any) -> ObservedSizing:
    """Kafka's size as its cluster metadata reports it."""
    return ObservedSizing(brokers=observed.kafka_brokers(settings))


def _observe_clickhouse(settings: Any) -> ObservedSizing:
    """ClickHouse's size as the server reports it, plus the Cloud service object where configured.

    Both reads are bounded by ``clickhouse.interactive_budget_seconds``, because a
    console request waits on them.
    """
    budget = settings.clickhouse.interactive_budget_seconds
    topology = observed.clickhouse_topology(lambda: get_interactive_clickhouse_client(settings))
    service = observed.cloud_service(settings.clickhouse.cloud, timeout=budget)
    return ObservedSizing(
        replicas=topology.replicas,
        shards=topology.shards,
        memory_bytes=topology.memory_bytes,
        provider_replicas=service.num_replicas if service else None,
        provider_min_replica_memory_gb=service.min_replica_memory_gb if service else None,
        provider_max_replica_memory_gb=service.max_replica_memory_gb if service else None,
        provider_idle_scaling=service.idle_scaling if service else None,
    )


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
        reload_by_segment={
            # The ClickHouse operator takes no new disk on an existing cluster,
            # and every model but `local` adds or re-shapes a volume claim.
            "storageModel": BackingReload.RECREATE,
            "tieredBlock": BackingReload.RECREATE,
        },
        observe=_observe_clickhouse,
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
        reload_by_segment={
            # Strimzi reconciles tieredStorage onto the running Kafka CR and
            # rolls the brokers onto it -- no claim changes shape.
            "storageModel": BackingReload.ROLL,
            "tieredObject": BackingReload.ROLL,
        },
        observe=_observe_kafka,
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
# `storageModel` stay distinct. A service's own `reload_by_segment` wins where the
# two disagree; anything in neither falls to APPLY.
_RELOAD_SHARED: dict[str, BackingReload] = {
    # mode=external removes the store's objects; mode=cluster creates them.
    "mode": BackingReload.REDEPLOY,
    # Server or broker config -- the operator restarts pods onto the new settings.
    "objectStore": BackingReload.ROLL,
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


class BackingFacts(BaseModel):
    """Who runs one backing service, from what the deployer states and the provider catalogue."""

    managed_by: ManagedBy = Field(
        description=(
            "dfe: DFE runs it and every declared value reaches it. provider: a SaaS "
            "whose API DFE knows runs it. external: someone else runs it, and DFE has "
            "no API to it. dfe wherever the deployer states no provider."
        ),
    )
    provider: str | None = Field(
        description="The provider key the deployer stated; null when it stated none."
    )
    tier: str | None = Field(
        description="The provider tier or instance type the deployer stated; null when none.",
    )
    region: str | None = Field(
        description="The provider region the deployer stated; null when none."
    )


class ObservedSizing(BaseModel):
    """A service's size as its own data plane reports it, held briefly per service.

    Shown, never acted on. A field is null where its read failed or does not apply
    to the service, so a down broker or server blanks it rather than failing the read.
    """

    brokers: int | None = Field(
        default=None, description="Kafka brokers the cluster metadata lists."
    )
    replicas: int | None = Field(
        default=None,
        description=(
            "ClickHouse replicas per shard in the cluster system.clusters lists; null "
            "where the server declares no cluster to count."
        ),
    )
    shards: int | None = Field(default=None, description="ClickHouse shards in that cluster.")
    memory_bytes: int | None = Field(
        default=None,
        description=(
            "Memory available to the ClickHouse server that answered: its cgroup "
            "limit, else the host total."
        ),
    )
    provider_replicas: int | None = Field(
        default=None,
        description=(
            "Replicas the ClickHouse Cloud service object states; null unless "
            "clickhouse.cloud is configured."
        ),
    )
    provider_min_replica_memory_gb: float | None = Field(
        default=None,
        description="The ClickHouse Cloud service object's minimum memory per replica, in GB.",
    )
    provider_max_replica_memory_gb: float | None = Field(
        default=None,
        description="The ClickHouse Cloud service object's maximum memory per replica, in GB.",
    )
    provider_idle_scaling: bool | None = Field(
        default=None,
        description="Whether the ClickHouse Cloud service idles when unused.",
    )


class ProviderLimit(BaseModel):
    """One cap the provider publishes for this tier, and how much of it is in use."""

    limit: str = Field(description="What is capped, e.g. partitions.")
    allowed: int = Field(description="The provider's published cap for this tier.")
    used: int | None = Field(
        default=None,
        description="What the data plane reports in use; null where it could not be read.",
    )
    source_url: str = Field(description="Where the provider publishes the cap.")
    as_of: str = Field(description="The date the cap was read, YYYY-MM-DD.")


class AppPrincipal(BaseModel):
    """The identity one app connects to the service as. Names only, never a secret."""

    app: str = Field(description="The DFE app, e.g. dfe-loader.")
    principal: str = Field(description="The user or service account it connects as.")
    mechanism: str = Field(description="How it authenticates, e.g. SCRAM-SHA-512.")


class BackingServiceConfig(BackingFacts):
    """A backing service's deploy configuration as DECLARED, and who runs it.

    A service DFE does not run also carries ``observed``, its size as its own data
    plane reports it, because the declared values do not reach it.
    """

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
    read_only: list[str] = Field(
        description=(
            "Value paths below prefix that a write is refused on with 409 "
            "managed_by_provider, at the path, below it or at a parent, because "
            "nothing in the deployment reads them. Empty where DFE runs the service."
        ),
    )
    observed: ObservedSizing | None = Field(
        description=(
            "The size the service's own data plane reports, read at most every "
            f"{observed.OBSERVED_TTL_SECONDS:g} seconds per service; null where DFE runs "
            "the service, whose declared values are the answer."
        ),
    )
    limits: list[ProviderLimit] = Field(
        description="The provider's caps for this tier; empty until the catalogue carries limits.",
    )
    principals: list[AppPrincipal] = Field(
        description="The identity each app connects as; empty until per-app identities deploy.",
    )


class BackingWriteResult(WriteResult):
    """A governed write plus what syncing it does to the running store."""

    reload: str = Field(
        description=(
            "'apply' reconciles in place, 'roll' restarts pods, 'recreate' needs the "
            "StatefulSet recreated by hand, 'redeploy' changes which objects exist."
        )
    )


def _sizing(request: Request) -> list[tuple[str, dict]]:
    """The resolver's data-chart sizing for this deployment's profile, where the deploy repo holds it.

    layer2-data renders it under ``infra/common.yaml`` and ``infra/<chart>.yaml``, so a
    count it sizes is the declared count until an overlay sets its own.
    """
    profile = request.app.state.settings.deployment.profile
    if not profile:
        return []
    gc = gitcrud_of(request)
    rel = f"{_SIZING_DIR}/{profile}{_SIZING_SUFFIX}"
    with gc.reading():
        f = gc.repo_path / rel
        if not f.is_file():
            return []
        return [(rel, yaml_load(f) or {})]


def _docs(request: Request, chart: str) -> list[tuple[str, dict]]:
    """The overlay documents that apply to one chart, lowest precedence first."""
    gc = gitcrud_of(request)
    out: list[tuple[str, dict]] = _sizing(request)
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


def _stated(request: Request, spec: BackingService) -> BackingFactSettings:
    """What the deployer states about this service."""
    return request.app.state.settings.deployment.backing.facts(spec.service)


def _resolution(
    spec: BackingService,
    stated: BackingFactSettings,
    docs: Callable[[], list[tuple[str, dict]]],
) -> Resolution:
    """Who runs this service. ``docs`` is read only where the mode decides it."""
    return providers.resolve(
        spec.service, stated.provider, lambda: _resolved(docs(), f"{spec.prefix}.mode")
    )


def _facts(stated: BackingFactSettings, resolution: Resolution) -> dict[str, Any]:
    """The ``BackingFacts`` fields, with an unstated fact as null rather than empty."""
    return {
        "managed_by": resolution.managed_by,
        "provider": stated.provider or None,
        "tier": stated.tier or None,
        "region": stated.region or None,
    }


def backing_facts(request: Request) -> dict[str, BackingFacts]:
    """Who runs each backing service, keyed by service, for GET /system/deployment.

    Blocking where a stated provider leaves the answer to the declared mode, which
    is read from the deploy repo; with gitops off nothing is declared.
    """
    has_repo = getattr(request.app.state, "gitcrud", None) is not None
    out: dict[str, BackingFacts] = {}
    for spec in CATALOGUE:
        stated = _stated(request, spec)

        def docs(chart: str = spec.chart) -> list[tuple[str, dict]]:
            return _docs(request, chart) if has_repo else []

        out[spec.service] = BackingFacts(**_facts(stated, _resolution(spec, stated, docs)))
    return out


def _config(request: Request, spec: BackingService) -> BackingServiceConfig:
    """One service's declared values, who runs it, and its observed size where DFE does not."""
    docs = _docs(request, spec.chart)

    def at(leaf: str) -> DeclaredValue:
        return _declared(request, docs, spec.chart, f"{spec.prefix}.{leaf}")

    stated = _stated(request, spec)
    resolution = _resolution(spec, stated, lambda: docs)
    return BackingServiceConfig(
        **_facts(stated, resolution),
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
        read_only=list(resolution.read_only),
        observed=_observed(request, spec, resolution),
        limits=[],
        principals=[],
    )


def _observed(
    request: Request, spec: BackingService, resolution: Resolution
) -> ObservedSizing | None:
    """A managed service's size off its data plane, held per service; None where DFE runs it.

    A self-run service's declared values are its size, so it is never read.
    """
    observe = spec.observe
    if observe is None or resolution.managed_by is ManagedBy.DFE:
        return None
    settings = request.app.state.settings
    cache: observed.SizingCache[ObservedSizing] = request.app.state.observed_sizing
    return cache.get(spec.service, lambda: observe(settings))


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
    segment = segments[1]
    if segment in spec.reload_by_segment:
        return spec.reload_by_segment[segment]
    return _RELOAD_SHARED.get(segment, BackingReload.APPLY)


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
    request: Request, spec: BackingService, name: str, edit: Callable[[dict], object]
) -> list[tuple[str, dict]]:
    """The overlay stack as it would read with ``edit`` applied to ``name``."""
    gc = gitcrud_of(request)
    out: list[tuple[str, dict]] = _sizing(request)
    for candidate in (_COMMON, spec.chart):
        try:
            doc = copy.deepcopy(gc.get(_CLASS, candidate))
        except ResourceNotFoundError:
            if candidate != name:
                continue
            doc = {}
        if candidate == name:
            edit(doc)
        out.append((candidate, doc))
    return out


def _guard_member_count(
    request: Request,
    name: str,
    path: str,
    edit: Callable[[dict], object],
    *,
    verb: str = "write",
    remedy: str = "",
) -> None:
    """Refuse an edit that lowers a declared node or broker count, or drops it.

    Compares the value the overlay stack declares BEFORE the edit with what it
    would declare after, so editing the shared file cannot be refused for a drop
    the per-chart file goes on to override anyway. Nothing declared means nothing
    to compare against, and the edit is accepted. An edit to a parent of a count
    is compared on every count below it, since it replaces or removes them; a
    declared count the edit leaves undeclared, or as anything but a count, hands
    the members to the chart default, which can be fewer.

    Args:
        request: The request carrying the gitops client.
        name: The overlay resource the edit lands in.
        path: The dot-path the edit sets or removes.
        edit: Applies the edit to a copy of ``name``'s document.
        verb: What the refusal calls the edit.
        remedy: Appended to the refusal after the service's scale-down reason.

    Raises:
        HTTPException: 400 ``scale_down_refused`` naming the count and the change.
    """
    spec = _owner(path)
    if spec is None or name not in (_COMMON, spec.chart):
        return
    counts = {f"{spec.prefix}.{leaf}" for leaf in (spec.replicas, *spec.extra_member_counts)}
    reached = sorted(c for c in counts if c == path or c.startswith(f"{path}."))
    if not reached:
        return

    current = _docs(request, spec.chart)
    edited = _stack_with(request, spec, name, edit)
    for count in reached:
        before = _resolved(current, count)
        if not _is_count(before):
            continue
        after = _resolved(edited, count)
        if _is_count(after) and after >= before:
            continue
        if _is_count(after):
            change = f"{before} -> {after} would remove a member"
        elif after is None:
            change = (
                f"this {verb} leaves it undeclared where {before} is declared, which hands "
                "it to the chart default and can remove members"
            )
        else:
            change = f"{before} -> {after!r} replaces a member count with something else"
        raise HTTPException(
            400,
            detail={
                "code": "scale_down_refused",
                "message": f"{count} is up-only: {change}. {spec.scale_down_reason}.{remedy}",
            },
        )


def _managed_message(
    spec: BackingService, path: str, stated: BackingFactSettings, resolution: Resolution
) -> str:
    """Why a managed value is refused, and where it is changed instead."""
    refused = f"{path} is not this deployment's to set"
    on_tier = f" on the {stated.tier} tier" if stated.tier else ""
    if resolution.set_in is SetIn.DEPLOYMENT:
        return (
            f"{refused}: {stated.provider} sizes {spec.service}{on_tier}. Change it in the "
            "deployment dial and redeploy, or change it in the provider."
        )
    if resolution.set_in is SetIn.PROVIDER:
        return (
            f"{refused}: {stated.provider} sizes {spec.service}{on_tier}. Change it in the "
            "provider's own console or API."
        )
    return (
        f"{refused}: DFE does not run this {spec.service} and has no API to it. Change it "
        "where the service is run."
    )


def _refuse_managed(request: Request, name: str, path: str) -> None:
    """Refuse a write to a value the deployment never reads, because DFE does not run the service.

    Runs before every other check, and helmvars:override does not lift it: the
    commit would land and change nothing, so it is a fact about the deployment
    rather than a policy. A write at a read-only path, below one, or at any parent
    of one is refused, in ``common`` or the service's own overlay. A service DFE
    runs refuses nothing here.

    Args:
        request: The request carrying the settings and the gitops client.
        name: The overlay resource the write lands in.
        path: The dot-path the write sets or removes.

    Raises:
        HTTPException: 409 ``managed_by_provider`` naming who sizes the value and where.
    """
    spec = _owner(path)
    if spec is None or name not in (_COMMON, spec.chart):
        return
    stated = _stated(request, spec)
    resolution = _resolution(spec, stated, lambda: _docs(request, spec.chart))
    relative = path.removeprefix(spec.prefix).removeprefix(".")
    if not resolution.refuses(relative):
        return
    raise HTTPException(
        409,
        detail={
            "code": MANAGED_REFUSAL,
            "message": _managed_message(spec, path, stated, resolution),
            "service": spec.service,
            "path": path,
            "managed_by": str(resolution.managed_by),
            "provider": stated.provider or None,
            "tier": stated.tier or None,
            "set_in": str(resolution.set_in) if resolution.set_in else None,
            "read_only": list(resolution.read_only),
        },
    )


def _replaced_by(document: dict) -> Callable[[dict], object]:
    """An edit that leaves a resource exactly as *document*."""

    def edit(doc: dict) -> None:
        doc.clear()
        doc.update(copy.deepcopy(document))

    return edit


def guard_action_change(request: Request, cls: str, name: str, path: str, document: dict) -> None:
    """Refuse an action change to the infra overlay that a write through this router would meet.

    A defined action writes the same overlay files, so it meets the same guards, in
    the same order, on what it leaves: a read-only value of a service DFE does not
    run first, then a declared node or broker count it lowers or un-declares.
    helmvars:override lifts neither, as on a set here: the first write would change
    nothing, and the second loses data rather than breaking a policy.

    Args:
        request: The request carrying the settings and the gitops client.
        cls: The class the change writes; any class but this router's is left alone.
        name: The overlay resource the change lands in.
        path: The dot-path the change sets.
        document: The resource as the whole action leaves it, every change applied.

    Raises:
        HTTPException: 409 ``managed_by_provider`` naming who sizes the value, or 400
            ``scale_down_refused`` naming the count and the change.
    """
    if cls != _CLASS:
        return
    _refuse_managed(request, name, path)
    _guard_member_count(request, name, path, _replaced_by(document), verb="action")


_WRITE_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorResponse,
        "description": (
            "managed_by_provider: DFE does not run the service, so nothing in the "
            "deployment reads this value; context names who sizes it and set_in where. "
            "Also conflict (a stale If-Match) and review_required (production+team)."
        ),
    },
}


@router.get("", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
def list_backing_services(user: CurrentUser, request: Request) -> list[BackingServiceConfig]:
    """Every backing service's DECLARED deploy configuration, and who runs it.

    Declared, not observed: these are the values the deploy repo asks for, read
    from git. The engine runs no Kubernetes client, so a figure here can differ
    from the cluster whenever Argo has not synced the declaration yet. A service
    DFE does not run (managed_by provider or external) adds ``observed``, read from
    its own broker or server and held briefly per service, and ``read_only``, the
    values a write is refused on.
    """
    return [_config(request, spec) for spec in CATALOGUE]


@router.get("/overlays", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
def list_overlays(user: CurrentUser, request: Request) -> list[str]:
    """List substrate/platform overlay resources in the deploy repo's infra/ dir."""
    return gitcrud_of(request).list(_CLASS)


@router.get(
    "/overlays/{name}/vars", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))]
)
def list_overlay_vars(name: str, user: CurrentUser, request: Request) -> list[dict[str, Any]]:
    """Flattened dot-path vars for one overlay, each marked protected or not.

    Credentials come back masked, as every other read of the deploy repo masks them.
    """
    check_name(name)
    gc = gitcrud_of(request)
    policy = policy_of(request)
    return [
        {
            "path": path,
            "value": value,
            "protected": bool(policy and policy.is_protected(_CLASS, name, path)),
        }
        for path, value in shown_vars(gc, _CLASS, name).items()
    ]


@router.get("/{service}", dependencies=[Depends(require_action(scopes_dict["helmvars_read"]))])
def get_backing_service(service: str, user: CurrentUser, request: Request) -> BackingServiceConfig:
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
    responses=_WRITE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
def set_overlay_var(
    name: str,
    path: str,
    body: SetVarRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> BackingWriteResult:
    """Set a substrate/platform value.

    409 managed_by_provider, before any other check, when DFE does not run the
    service and the value is one of its read_only paths; helmvars:override does not
    lift it. 403 with the policy that blocked it when the var is protected - the
    storage model, the data-layer modes and the disk size are decided at deploy, and
    moving one on a live deployment is a data migration. 400 when the value would
    lower a declared node or broker count, or leave it undeclared (a parent map that
    omits it), which loses data rather than capacity; helmvars:override does not lift it.
    """
    check_name(name)
    _refuse_managed(request, name, path)
    _guard_member_count(request, name, path, lambda doc: set_path(doc, path, body.value))
    result = set_var_governed(_CLASS, name, path, body.value, user, request, if_match)
    return BackingWriteResult(**result.model_dump(), reload=str(_reload_for(path)))


@router.delete(
    "/overlays/{name}/vars/{path}",
    response_model=BackingWriteResult,
    responses=_WRITE_RESPONSES,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
def delete_overlay_var(
    name: str, path: str, user: CurrentUser, request: Request
) -> BackingWriteResult:
    """Revert a substrate/platform value to its chart default. Protected vars refuse.

    409 managed_by_provider, before any other check and whatever the caller holds,
    when DFE does not run the service and the value is one of its read_only paths.
    400 when the revert would lower a declared node or broker count, or leave it
    undeclared, unless the caller holds helmvars:override. An undeclared count falls
    to the chart or profile default, which the engine cannot read, so a revert that
    leaves one undeclared is refused whatever that default is.
    """
    check_name(name)
    _refuse_managed(request, name, path)
    if not has_override(request, user):
        # A revert reaching a policy lock refuses as protected_var before the count guard runs.
        enforce_protected(request, user, _CLASS, name, path)
        _guard_member_count(
            request,
            name,
            path,
            lambda doc: del_path(doc, path),
            verb="revert",
            remedy=" A revert that lowers or un-declares a count needs helmvars:override.",
        )
    result = delete_var_governed(_CLASS, name, path, user, request)
    return BackingWriteResult(**result.model_dump(), reload=str(_reload_for(path)))
