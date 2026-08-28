#  Project:      dfe-engine
#  File:         appmgmt/instances.py
#  Purpose:      App-instance identity and lifecycle over the helmvars overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An app instance IS its values-overlay file, and nothing else.

``values/{service}-{instance}-values.yaml`` in the deploy repo is what the
layer2-apps ApplicationSet globs to produce Argo Applications, so creating the file
deploys the instance and deleting it undeploys it. There is no separate registry to
drift from git.

The instance name is load-bearing three times over: it keys the file, it names the
Argo Application, and it becomes the app's OTel ``service.name`` - so it is validated
as a DNS label rather than merely as a safe filename.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.engine import ResourceNotFoundError, set_path
from dfe_engine.gitcrud.log import LogEntry, read_log

from .catalogue import (
    COMPONENT_PATH,
    DEPLOY_INSTANCE_PATH,
    DEPLOY_SERVICE_PATH,
    OTEL_SERVICE_NAME_PATH,
    Multiplicity,
    descriptor,
    render_source_binding,
    services,
)

HELMVARS_CLASS = "helmvars"

_OVERLAY_SUFFIX = "-values"

# A DNS-1123 label: the instance ends up in Argo Application and Kubernetes object
# names, so anything wider than this fails at deploy time rather than here.
_INSTANCE_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?")


class InvalidInstanceError(ValueError):
    """Raised when an instance name would not survive as a DNS label."""


class InstanceExistsError(ValueError):
    """Raised when creating an instance whose overlay is already present."""


@dataclass(frozen=True, slots=True)
class AppInstance:
    """One deployed app instance."""

    service: str
    instance: str

    @property
    def overlay_name(self) -> str:
        """The gitcrud resource name for this instance's overlay."""
        return f"{self.service}-{self.instance}{_OVERLAY_SUFFIX}"

    @property
    def telemetry_name(self) -> str:
        """The OTel ``service.name`` that distinguishes this instance's metrics."""
        return f"{self.service}-{self.instance}"

    @property
    def component(self) -> str:
        """The chart's component name for this service, without the project prefix."""
        return self.service.removeprefix("dfe-")


def validate_instance(instance: str) -> None:
    """Reject an instance name that would not survive as a DNS label."""
    if not _INSTANCE_RE.fullmatch(instance):
        raise InvalidInstanceError(
            f"invalid instance name {instance!r}: expected a DNS-1123 label "
            "(lowercase alphanumeric and '-', starting and ending alphanumeric, "
            "40 characters or fewer)"
        )


def instance_of(service: str, instance: str) -> AppInstance:
    """Build a validated instance identity."""
    descriptor(service)
    validate_instance(instance)
    return AppInstance(service=service, instance=instance)


def parse_overlay_name(name: str) -> AppInstance | None:
    """Recover the instance identity from an overlay resource name.

    Returns None for a values file that does not belong to a catalogued app, so a
    hand-written overlay for something else is listed by the raw helm API but does
    not masquerade as a managed instance here.
    """
    if not name.endswith(_OVERLAY_SUFFIX):
        return None
    stem = name[: -len(_OVERLAY_SUFFIX)]
    # Longest service name first: dfe-transform-vrl must win over any shorter
    # service that happens to be a prefix of it.
    for service in sorted(services(), key=len, reverse=True):
        prefix = f"{service}-"
        if stem.startswith(prefix):
            instance = stem[len(prefix) :]
            if instance and _INSTANCE_RE.fullmatch(instance):
                return AppInstance(service=service, instance=instance)
    return None


def list_instances(gc: GitCrud, service: str | None = None) -> list[AppInstance]:
    """Every managed instance present in the deploy repo, optionally one service's."""
    found = (parse_overlay_name(name) for name in gc.list(HELMVARS_CLASS))
    return sorted(
        (i for i in found if i is not None and (service is None or i.service == service)),
        key=lambda i: (i.service, i.instance),
    )


def additional_instance_allowed(gc: GitCrud, app: AppInstance) -> tuple[bool, str]:
    """Whether this app's shape permits another instance alongside the ones deployed.

    A scale-pool app is one config scaled by KEDA, and its chart names Kubernetes
    objects from the component alone, so a second config would render the same object
    names and the two Argo Applications would fight over them under self-heal.
    """
    if descriptor(app.service).multiplicity is not Multiplicity.SINGLE:
        return True, ""
    deployed = [i for i in list_instances(gc, service=app.service) if i != app]
    if not deployed:
        return True, ""
    running = ", ".join(i.instance for i in deployed)
    return False, (
        f"{app.service} runs one deployment for the whole stack (already deployed: "
        f"{running}). Raise its replica ceiling rather than deploying another."
    )


def exists(gc: GitCrud, app: AppInstance) -> bool:
    """Whether this instance's overlay is present."""
    try:
        gc.get(HELMVARS_CLASS, app.overlay_name)
    except ResourceNotFoundError:
        return False
    return True


def overlay_file(gc: GitCrud, app: AppInstance) -> str:
    """Repo-relative path of the instance's overlay, as the git log reports it."""
    cls = gc.resource_class(HELMVARS_CLASS)
    return f"{cls.directory}/{app.overlay_name}{cls.suffix}"


def history(
    gc: GitCrud,
    app: AppInstance,
    *,
    applied_revision: str | None = None,
    limit: int = 20,
) -> list[LogEntry]:
    """Commits touching this instance's overlay, newest first.

    Every mutation to an instance is a commit, so this is its whole audit trail.
    Passing the revision Argo has synced marks each entry ``applied`` or
    ``pending`` instead of the bare ``committed``, which is what tells a caller
    whether a change has actually reached the cluster yet.
    """
    path = overlay_file(gc, app)
    # Over-read, because the walk is repo-wide and most commits touch other files.
    entries, _ = read_log(gc, limit=limit * 20, applied_revision=applied_revision)
    return [e for e in entries if path in e.files][:limit]


def read_overlay(gc: GitCrud, app: AppInstance) -> dict:
    """The instance's overlay document."""
    return gc.get(HELMVARS_CLASS, app.overlay_name)


def initial_overlay(app: AppInstance, values: dict | None = None) -> dict:
    """The minimum document that makes an instance deployable.

    The ``deploy`` block is what the ApplicationSet reads to name the Application;
    without it the file produces nothing. The OTel service name is set here because
    it is the only thing that makes this instance's telemetry distinguishable from
    another instance of the same app.
    """
    desc = descriptor(app.service)
    doc: dict = {}
    set_path(doc, DEPLOY_SERVICE_PATH, app.service)
    set_path(doc, DEPLOY_INSTANCE_PATH, app.instance)
    set_path(doc, OTEL_SERVICE_NAME_PATH, app.telemetry_name)
    if desc.component_is_per_instance:
        set_path(doc, COMPONENT_PATH, f"{app.component}-{app.instance}")
    # A source-bound app's instance IS the source, so the binding is derived from
    # the instance name rather than asked for separately.
    for path, value in render_source_binding(desc, app.instance).items():
        set_path(doc, path, value)
    for path, value in (values or {}).items():
        set_path(doc, path, value)
    return doc
