#  Project:      dfe-engine
#  File:         appmgmt/scaling.py
#  Purpose:      The scaling dials - a typed facade over the same helm vars
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CPU, memory, the replica count and the KEDA range, as one validated surface.

These dials are not a second store: they ARE helm values, written through the same
gitcrud path as any other overlay change. This module only decides which paths make
up the surface, what a legal value is, and when the surface applies at all.

Off Kubernetes the dials are reported unsupported rather than hidden: a Compose
deployment has no KEDA and only stack-wide CPU and memory limits, so the caller
needs to render them disabled with a reason rather than have them vanish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from dfe_engine.gitcrud.engine import get_path

from .catalogue import (
    CPU_LIMIT_PATH,
    CPU_REQUEST_PATH,
    KEDA_ENABLED_PATH,
    KEDA_MAX_PATH,
    KEDA_MIN_PATH,
    MEMORY_LIMIT_PATH,
    MEMORY_REQUEST_PATH,
    REPLICA_COUNT_PATH,
    AppDescriptor,
    Multiplicity,
)

# Kubernetes quantity forms we accept. CPU is either a plain number of cores or
# milli-cores; memory is a byte count with an optional binary or decimal suffix.
_CPU_RE = re.compile(r"(\d+(\.\d+)?|\d+m)")
_MEMORY_RE = re.compile(r"\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?")

MAX_REPLICAS_CEILING = 1000
"""Refuse a replica ceiling above this - a typo here bankrupts a cluster."""


class DeployTarget(StrEnum):
    """Where this DFE deployment runs."""

    KUBERNETES = "kubernetes"
    DOCKER = "docker"
    UNKNOWN = "unknown"


class InvalidDialError(ValueError):
    """Raised when a dial value is not a legal Kubernetes quantity or range."""


@dataclass(frozen=True, slots=True)
class ScalingDials:
    """The scaling surface for one app instance."""

    supported: bool
    reason: str = ""
    replica_count: int | None = None
    min_replicas: int | None = None
    max_replicas: int | None = None
    keda_enabled: bool | None = None
    cpu_request: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


def instance_ceiling(
    app: AppDescriptor, target: DeployTarget, *, writes_app_config: bool = False
) -> int | None:
    """How many deployments of *app* this target can RUN, None meaning unbounded.

    Read off the manifest's multiplicity rather than a list of app names, so an
    app becomes per-config by being declared one.

    A single-deployment app is one everywhere. A per-config app is unbounded on
    Kubernetes, where each overlay renders its own Argo Application, and equally
    unbounded on Compose WHERE THE ENGINE RENDERS THE APP CONFIG: each instance
    gets its own directory and its name goes to the deployer's instance index, so
    the deployer declares a container per instance on the next up. Where nothing
    renders it, Compose holds only the services its committed file declares and
    the source would be saved and never run, which is a ceiling of zero. A target
    nobody named is not assumed to be Compose.

    Args:
        app: The app the ceiling is being asked about.
        target: Where this deployment runs.
        writes_app_config: Whether the engine renders the apps' config files
            here, which is ``appconfig.enabled``.
    """
    if app.multiplicity is not Multiplicity.PER_CONFIG:
        return 1
    if target is not DeployTarget.DOCKER:
        return None
    return None if writes_app_config else 0


def support(app: AppDescriptor, target: DeployTarget) -> tuple[bool, str]:
    """Whether the dials apply here, and why not when they do not."""
    if not app.scale_deployed:
        return False, f"{app.service} is not a scale-deployed app"
    if target is DeployTarget.KUBERNETES:
        return True, ""
    if target is DeployTarget.DOCKER:
        return False, (
            "deploy target is docker: Compose has no KEDA, and CPU and memory are "
            "set stack-wide rather than per component"
        )
    return False, "deploy target is unknown, so scaling dials cannot be applied safely"


def read(doc: dict, app: AppDescriptor, target: DeployTarget) -> ScalingDials:
    """The instance's dials as the overlay currently sets them.

    A ``None`` means the overlay does not set that dial, so the effective value comes
    from the chart default or the profile layer beneath it - which is a different
    thing from being set to that value, and the caller needs to be able to tell.
    """
    supported, reason = support(app, target)
    if not supported:
        return ScalingDials(supported=False, reason=reason)
    return ScalingDials(
        supported=True,
        replica_count=get_path(doc, REPLICA_COUNT_PATH),
        min_replicas=get_path(doc, KEDA_MIN_PATH),
        max_replicas=get_path(doc, KEDA_MAX_PATH),
        keda_enabled=get_path(doc, KEDA_ENABLED_PATH),
        cpu_request=_as_str(get_path(doc, CPU_REQUEST_PATH)),
        memory_request=_as_str(get_path(doc, MEMORY_REQUEST_PATH)),
        cpu_limit=_as_str(get_path(doc, CPU_LIMIT_PATH)),
        memory_limit=_as_str(get_path(doc, MEMORY_LIMIT_PATH)),
    )


def changes(
    doc: dict,
    *,
    replica_count: int | None = None,
    min_replicas: int | None = None,
    max_replicas: int | None = None,
    keda_enabled: bool | None = None,
    cpu_request: str | None = None,
    memory_request: str | None = None,
    cpu_limit: str | None = None,
    memory_limit: str | None = None,
) -> dict[str, object]:
    """Validate a dial update and return the helm-var changes it implies.

    Only the fields the caller supplied are returned, so a partial update leaves
    every other dial alone. ``doc`` supplies the current values the new range is
    checked against, so raising only the ceiling still validates against the
    existing floor.
    """
    out: dict[str, object] = {}

    if keda_enabled is not None:
        out[KEDA_ENABLED_PATH] = bool(keda_enabled)

    effective_min = min_replicas if min_replicas is not None else get_path(doc, KEDA_MIN_PATH)
    effective_max = max_replicas if max_replicas is not None else get_path(doc, KEDA_MAX_PATH)
    effective_keda = keda_enabled if keda_enabled is not None else get_path(doc, KEDA_ENABLED_PATH)

    if replica_count is not None:
        if replica_count < 0:
            raise InvalidDialError(f"replica_count cannot be negative, got {replica_count}")
        if replica_count > MAX_REPLICAS_CEILING:
            raise InvalidDialError(
                f"replica_count {replica_count} exceeds the {MAX_REPLICAS_CEILING} ceiling"
            )
        # The charts omit `replicas:` while KEDA owns the count. Only an explicit
        # `true` refuses -- an unset key is the chart default, which is not readable
        # from here.
        if effective_keda is True:
            raise InvalidDialError(
                "replica_count does not apply while KEDA is enabled: the chart omits "
                "replicas and the ScaledObject owns the count. Set keda_enabled false "
                "in the same request, or move min_replicas instead"
            )
        out[REPLICA_COUNT_PATH] = replica_count

    if min_replicas is not None:
        if min_replicas < 1:
            raise InvalidDialError(
                f"min_replicas must be at least 1, got {min_replicas}: KEDA scales to "
                "zero only with an idleReplicaCount, which this surface does not set"
            )
        out[KEDA_MIN_PATH] = min_replicas

    if max_replicas is not None:
        if max_replicas > MAX_REPLICAS_CEILING:
            raise InvalidDialError(
                f"max_replicas {max_replicas} exceeds the {MAX_REPLICAS_CEILING} ceiling"
            )
        out[KEDA_MAX_PATH] = max_replicas

    if isinstance(effective_min, int) and isinstance(effective_max, int):
        if effective_max < effective_min:
            raise InvalidDialError(
                f"max_replicas {effective_max} is below min_replicas {effective_min}"
            )

    for value, path, kind in (
        (cpu_request, CPU_REQUEST_PATH, "cpu"),
        (cpu_limit, CPU_LIMIT_PATH, "cpu"),
        (memory_request, MEMORY_REQUEST_PATH, "memory"),
        (memory_limit, MEMORY_LIMIT_PATH, "memory"),
    ):
        if value is None:
            continue
        _validate_quantity(value, kind)
        out[path] = value

    return out


def _validate_quantity(value: str, kind: str) -> None:
    pattern = _CPU_RE if kind == "cpu" else _MEMORY_RE
    if not pattern.fullmatch(value):
        expected = "'2', '0.5' or '500m'" if kind == "cpu" else "'512Mi', '2Gi' or '1000000'"
        raise InvalidDialError(f"invalid {kind} quantity {value!r}: expected {expected}")


def _as_str(value: object) -> str | None:
    return None if value is None else str(value)
