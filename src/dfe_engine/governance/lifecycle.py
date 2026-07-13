#  Project:      dfe-engine
#  File:         governance/lifecycle.py
#  Purpose:      Service lifecycle (start/stop/pause) via a gitops state dial
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Start / stop / pause any DFE service through one gitops state dial.

Each service declares a TIER: managed-app (operator-grantable), managed-backing
(admin/infra roles only), or pinned (no lifecycle via the API - the critical
cascade deps: the cluster, DNS, ClickHouse, the secrets manager, Argo). The API
writes a ``state`` value (running | paused | stopped) to the deploy repo via
GitCrud and Argo reconciles - nothing is applied live, the engine only flips a
config dial (see docs/deployment/backing-services.md and the engine<->infra boundary).

State semantics:
  running - deployed; KEDA scales min..max.
  paused  - replicas pinned to 0, config/Application intact (resumable instantly;
            safe because dfe-* apps are crash-safe - CH watermarks, Kafka retention).
  stopped - disabled stub kept in git (nothing deleted; gitops-survivable).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dfe_engine.gitcrud import GitCrud
    from dfe_engine.gitops.repo import PublishResult


class ServiceTier(StrEnum):
    """Who may drive a service's lifecycle (and whether the API exposes it)."""

    MANAGED_APP = "managed_app"  # dfe-* app: operator-grantable
    MANAGED_BACKING = "managed_backing"  # backing service: admin/infra roles only
    PINNED = "pinned"  # critical cascade dep: no lifecycle via the API


class LifecycleState(StrEnum):
    """The gitops state dial value."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class LifecycleError(Exception):
    """A lifecycle operation that is not permitted (e.g. a pinned service)."""


@dataclass(frozen=True, slots=True)
class ServiceLifecycle:
    """A service's lifecycle descriptor: tier + where its dial lives in gitops."""

    name: str
    tier: ServiceTier
    resource: str  # the gitops resource holding the dial (e.g. the app overlay)
    cls: str = "helmvars"  # the GitCrud resource class
    path: str = "state"  # the dot-path of the dial within the resource


# Built-in registry. dfe-* apps are operator-managed; a few backing services are
# admin/infra-managed; critical cascade deps are pinned. Overridable from gitops
# later (YAGNI: built-in for now).
_MANAGED_APPS = ("receiver", "loader", "fetcher", "archiver", "transforms", "hunt-runner", "ui")
_MANAGED_BACKING = ("kafka", "objectstore", "hyperdx")
_PINNED = ("clickhouse", "openbao", "dns", "k8s", "argo", "cert-manager", "eso")

_REGISTRY: dict[str, ServiceLifecycle] = {}
for _n in _MANAGED_APPS:
    _REGISTRY[_n] = ServiceLifecycle(_n, ServiceTier.MANAGED_APP, resource=_n)
for _n in _MANAGED_BACKING:
    _REGISTRY[_n] = ServiceLifecycle(_n, ServiceTier.MANAGED_BACKING, resource=_n)
for _n in _PINNED:
    _REGISTRY[_n] = ServiceLifecycle(_n, ServiceTier.PINNED, resource=_n)


def resolve(name: str) -> ServiceLifecycle:
    """Return a service's lifecycle descriptor. Raises KeyError if unknown."""
    if name not in _REGISTRY:
        raise KeyError(name)
    return _REGISTRY[name]


def services() -> list[ServiceLifecycle]:
    """All known services and their tiers (sorted by name)."""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def required_action(name: str, state: LifecycleState) -> str:
    """The RBAC action needed to set ``state`` on ``name``.

    Apps -> ``lifecycle:app:<name>`` (operator-grantable); backing services ->
    ``lifecycle:backing:<name>`` (admin/infra roles only). Raises LifecycleError for
    a pinned service - it has no lifecycle action at all.
    """
    svc = resolve(name)
    if svc.tier == ServiceTier.PINNED:
        raise LifecycleError(f"'{name}' is pinned - no lifecycle via the API")
    scope = "app" if svc.tier == ServiceTier.MANAGED_APP else "backing"
    return f"lifecycle:{scope}:{name}"


def set_state(
    crud: GitCrud,
    name: str,
    state: LifecycleState,
    actor: str,
    base_revision: str | None = None,
) -> PublishResult:
    """Write a service's lifecycle state to gitops (a helm-var dial) and commit.

    Raises LifecycleError for a pinned service. The dial is committed to the deploy
    repo; Argo reconciles it (commit != deployment). Returns the PublishResult
    (commit sha), which the API surfaces to the caller.
    """
    svc = resolve(name)
    if svc.tier == ServiceTier.PINNED:
        raise LifecycleError(f"'{name}' is pinned - no lifecycle via the API")
    msg = f"lifecycle({name}): {state.value} by {actor}"
    return crud.set_key(
        svc.cls,
        svc.resource,
        svc.path,
        state.value,
        actor,
        message=msg,
        base_revision=base_revision,
    )
