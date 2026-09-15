#  Project:      dfe-engine
#  File:         appmgmt/derived.py
#  Purpose:      Keep the deploy repo's derived app state in step with the sources
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the sources imply about deployed apps, applied on every source change.

Two things in the deploy repo are DERIVED from the source definitions and must
never be authored by hand:

- The routing block of every stack-scoped app (the receiver's source rules, the
  loader's table map, the archiver's topic list). A source deploy that does not
  push it leaves the receiver on built-in defaults, routing every event to the
  default topic.
- The instances of every instance-scoped app: a fetcher per fetcher-based
  source, a transform per source that names one. An active, deployed source the
  app has something to do for means an instance named for it exists with the
  source's block compiled in; anything else means no instance. Which apps those
  are is the manifest's to say, so a new one is a manifest edit.

``plan`` reads the deploy repo and the sources and lists the overlay writes that
bring the two into step. It never writes: the API applies each change through
the governed write path so review routing and audit stay where they are.
``reconcile`` applies the plan as the engine itself, at startup: a fresh deploy
seeds every default app with no routing at all, and until the main flow is
compiled in, a receiver with no destination refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.source.registry import SourceRegistry

from . import catalogue, instances, routing
from .instances import AppInstance

DerivedAction = Literal["deploy", "sync", "remove"]

ENGINE_ACTOR = "dfe-engine"


@dataclass(frozen=True, slots=True)
class DerivedChange:
    """One overlay write the sources call for."""

    app: AppInstance
    action: DerivedAction
    doc: dict[str, Any] | None
    """The overlay to commit; None for a removal."""

    @property
    def summary(self) -> str:
        """The commit summary for this change."""
        return {
            "deploy": "deploy instance",
            "sync": "sync routing",
            "remove": "undeploy instance",
        }[self.action]

    def describe(self) -> str:
        """One line for a report: ``dfe-fetcher/okta-audit: deploy instance``."""
        return f"{self.app.service}/{self.app.instance}: {self.summary}"


def plan(gc: GitCrud, registry: SourceRegistry, settings: Any) -> list[DerivedChange]:
    """Every overlay write that brings the deploy repo into step with the sources."""
    deployed = instances.list_instances(gc)
    docs = {app: instances.read_overlay(gc, app) for app in deployed}
    changes: list[DerivedChange] = []

    for app, doc in docs.items():
        desc = app.descriptor
        if not desc.has_compiled_routing or desc.routing_is_per_instance:
            continue
        if routing.sync(desc, doc, registry, settings):
            changes.append(DerivedChange(app, "sync", doc))

    # An instance runs only for a source that is active AND deployed: before the
    # deploy there is no table for its records to land in.
    live = [s for s in registry.get_all_sources(states=("active",)) if s.deployed_version]
    for desc in catalogue.instance_routed_apps():
        existing = {app.instance: app for app in deployed if app.service == desc.service}
        wanted: dict[str, dict[str, Any]] = {}
        for source in live:
            try:
                compiled = routing.compile_for(desc, registry, settings, instance=source.source)
            except routing.RoutingNotApplicableError:
                continue
            wanted[source.source] = compiled

        for name, compiled in wanted.items():
            if name in existing:
                doc = docs[existing[name]]
                if routing.sync(desc, doc, registry, settings, instance=name):
                    changes.append(DerivedChange(existing[name], "sync", doc))
                continue
            app = instances.instance_of(desc.service, name)
            doc = instances.initial_overlay(app)
            routing.apply(desc, doc, compiled)
            changes.append(DerivedChange(app, "deploy", doc))

        for name, app in existing.items():
            if name not in wanted:
                changes.append(DerivedChange(app, "remove", None))

    return changes


def reconcile(
    gc: GitCrud, registry: SourceRegistry, settings: Any, actor: str = ENGINE_ACTOR
) -> list[str]:
    """Apply the plan straight to the tracked branch and report each write.

    Derived state is never hand-authored, so it takes no review: this is the
    same commit shape the API makes, signed by the engine rather than a user.
    """
    done: list[str] = []
    for change in plan(gc, registry, settings):
        scope, summary = instances.fit_subject(change.app, change.summary)
        message = build_message(
            CommitContext(
                ctype="cfg", scope=scope, summary=summary, actor=actor, role="helmvars:write"
            )
        )
        name = change.app.overlay_name
        if change.action == "remove":
            gc.delete(instances.HELMVARS_CLASS, name, actor, message=message)
        else:
            gc.put(instances.HELMVARS_CLASS, name, change.doc or {}, actor, message=message)
        done.append(change.describe())
    return done
