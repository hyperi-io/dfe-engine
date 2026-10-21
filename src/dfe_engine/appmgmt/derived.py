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

The files an operator authored into an instance's file sets are not derived, so
removing an instance for a source that still exists keeps them in a parked copy
the next deploy of that instance restores. A deleted source takes its parked copy
with it.
"""

from dataclasses import dataclass
from typing import Any, Literal

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.engine import ResourceNotFoundError
from dfe_engine.source.reader import SourceReader

from . import catalogue, files, instances, routing
from .instances import AppInstance

DerivedAction = Literal["deploy", "sync", "remove", "forget"]

ENGINE_ACTOR = "dfe-engine"

PARK_SUMMARY = "park authored files"
UNPARK_SUMMARY = "drop parked files"


@dataclass(frozen=True, slots=True)
class DerivedChange:
    """One overlay write the sources call for."""

    app: AppInstance
    action: DerivedAction
    doc: dict[str, Any] | None
    """The overlay to commit, and None for a removal or a forget."""

    park: dict[str, Any] | None = None
    """For a removal, the authored files to keep in the instance's parked copy."""

    unpark: bool = False
    """Whether the instance's parked copy goes with this change."""

    @property
    def summary(self) -> str:
        """The commit summary for this change."""
        return {
            "deploy": "deploy instance",
            "sync": "sync routing",
            "remove": "undeploy instance",
            "forget": UNPARK_SUMMARY,
        }[self.action]

    def describe(self) -> str:
        """One line for a report: ``dfe-fetcher/okta-audit: deploy instance``."""
        return f"{self.app.service}/{self.app.instance}: {self.summary}"


def plan(gc: GitCrud, registry: SourceReader, settings: Any) -> list[DerivedChange]:
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
    known = {s.source for s in registry.get_all_sources()}
    parked = {
        app: gc.get(instances.HELMVARS_CLASS, app.parked_name) for app in instances.list_parked(gc)
    }
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
            kept = parked.get(app)
            if kept is not None:
                files.restore_authored_content(desc.files, doc, kept)
            changes.append(DerivedChange(app, "deploy", doc, unpark=kept is not None))

        for name, app in existing.items():
            if name in wanted:
                continue
            # A deleted source takes its authored files with it.
            authored = files.authored_content(desc.files, docs[app]) if name in known else {}
            changes.append(
                DerivedChange(app, "remove", None, park=authored or None, unpark=app in parked)
            )

    for app in parked:
        if app.instance not in known:
            changes.append(DerivedChange(app, "forget", None, unpark=True))

    return changes


def reconcile(
    gc: GitCrud, registry: SourceReader, settings: Any, actor: str = ENGINE_ACTOR
) -> list[str]:
    """Apply the plan straight to the tracked branch and report each write.

    Derived state is never hand-authored, so it takes no review: this is the
    same commit shape the API makes, signed by the engine rather than a user.
    """
    done: list[str] = []
    for change in plan(gc, registry, settings):
        message = _message(change.app, change.summary, actor)
        name = change.app.overlay_name
        if change.action == "forget":
            drop_parked(gc, change.app, actor)
        elif change.action == "remove":
            keep_parked(gc, change, actor)
            gc.delete(instances.HELMVARS_CLASS, name, actor, message=message)
        else:
            gc.put(instances.HELMVARS_CLASS, name, change.doc or {}, actor, message=message)
            if change.unpark:
                drop_parked(gc, change.app, actor)
        done.append(change.describe())
    return done


def _message(app: AppInstance, summary: str, actor: str) -> str:
    scope, subject = instances.fit_subject(app, summary)
    return build_message(
        CommitContext(ctype="cfg", scope=scope, summary=subject, actor=actor, role="helmvars:write")
    )


def keep_parked(gc: GitCrud, change: DerivedChange, actor: str) -> None:
    """Write the parked copy a removal keeps, or drop a stale one, ahead of the removal itself.

    Committed straight to the tracked branch whatever the overlay write's routing: the
    copy deploys nothing, and holds only what the overlay already carried.
    """
    if change.park:
        gc.put(
            instances.HELMVARS_CLASS,
            change.app.parked_name,
            change.park,
            actor,
            message=_message(change.app, PARK_SUMMARY, actor),
        )
    elif change.unpark:
        drop_parked(gc, change.app, actor)


def drop_parked(gc: GitCrud, app: AppInstance, actor: str) -> None:
    """Delete the instance's parked copy, where another write has not already."""
    try:
        gc.delete(
            instances.HELMVARS_CLASS,
            app.parked_name,
            actor,
            message=_message(app, UNPARK_SUMMARY, actor),
        )
    except ResourceNotFoundError:
        return
