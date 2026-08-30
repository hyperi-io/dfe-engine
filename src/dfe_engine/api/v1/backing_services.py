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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud.engine import ResourceNotFoundError, get_path

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


CATALOGUE: tuple[BackingService, ...] = (
    BackingService(
        service="clickhouse",
        chart="clickhouse-cluster",
        prefix="clickhouse",
        replicas="replicas",
        storage_size="storage.size",
        storage_class="storage.storageClass",
    ),
    BackingService(
        service="kafka",
        chart="kafka",
        prefix="kafka",
        replicas="replicas",
        storage_size="storage.size",
        storage_class="storage.storageClass",
    ),
)

_BY_SERVICE = {b.service: b for b in CATALOGUE}

_RESOURCE_PATHS = (
    "resources.requests.cpu",
    "resources.requests.memory",
    "resources.limits.cpu",
    "resources.limits.memory",
)


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
    mode: DeclaredValue
    storage_model: DeclaredValue
    replicas: DeclaredValue
    storage_size: DeclaredValue
    storage_class: DeclaredValue
    resources: dict[str, DeclaredValue] = Field(default_factory=dict)


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
        mode=at("mode"),
        storage_model=at("storageModel"),
        replicas=at(spec.replicas),
        storage_size=at(spec.storage_size),
        storage_class=at(spec.storage_class),
        resources={p: at(p) for p in _RESOURCE_PATHS},
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
    response_model=WriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def set_overlay_var(
    name: str,
    path: str,
    body: SetVarRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Set a substrate/platform value. 403 with the policy that blocked it when the
    var is protected - the storage model and the data-layer modes are decided at
    deploy, and moving one on a live deployment is a data migration.
    """
    return set_var_governed(_CLASS, name, path, body.value, user, request, if_match)


@router.delete(
    "/overlays/{name}/vars/{path}",
    response_model=WriteResult,
    dependencies=[Depends(require_action(scopes_dict["helmvars_write"]))],
)
async def delete_overlay_var(
    name: str, path: str, user: CurrentUser, request: Request
) -> WriteResult:
    """Revert a substrate/platform value to its chart default. Protected vars refuse."""
    return delete_var_governed(_CLASS, name, path, user, request)
