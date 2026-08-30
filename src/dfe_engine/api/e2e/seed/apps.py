#  Project:      dfe-engine
#  File:         api/e2e/seed/apps.py
#  Purpose:      App-instance seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""App-instance seeding. Public methods are the scripts; privates are reusable.

An instance IS its values overlay, so every method here writes one overlay through
the same ``appmgmt`` document operations ``api/v1/apps.py`` uses and commits it with
``GitCrud.put``. Seeding is bootstrap rather than an operator edit, so it skips the
review routing that router applies; everything it produces is read back through the
product API unchanged.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import SEED_ACTOR, SEED_SOURCE_NAME
from dfe_engine.appmgmt import catalogue, files, instances, routing, scaling
from dfe_engine.gitcrud.engine import set_path

TRANSFORM_SERVICE = "dfe-transform-vrl"
"""The transform an instance of which IS a source's processing step."""

TRANSFORM_FILE_SET = "transforms"
"""The file set the seeded program and the seeded library link land in."""

FETCHER_SERVICE = "dfe-fetcher"
POOL_SERVICES = ("dfe-receiver", "dfe-loader")
POOL_INSTANCE = "default"
"""Instance name for a single-multiplicity app: one deployment for the whole stack."""

RUN_STATE_CLASSES = ("infravars", "gov_settings")
"""Deploy-repo classes a reset clears, beyond the ones the child seeders own.

Both are per-run state on the same three tests: nothing seeds them at startup,
each is only written through an API a spec drives, and its absence IS the shipped
default. ``infravars`` is the substrate overlay (backing-service node counts,
storage, CPU); ``gov_settings`` is the auto-merge posture flag.

Deliberately NOT here: ``actions`` and ``policies`` are the shipped governance
library, re-seeded from packaged resources on every start; ``accounts`` is the
durable break-glass copy the reset re-mirrors rather than removes.
"""

_TRANSFORM_FILENAME = f"{SEED_SOURCE_NAME}.vrl"
_TRANSFORM_PROGRAM = (
    f'# Seeded transform program.\n.dfe_seeded = true\n.dfe_source = "{SEED_SOURCE_NAME}"\n'
)

# Non-default dials, so the Components page renders a set surface rather than the
# chart defaults it cannot distinguish from an unset one.
_MIN_REPLICAS = 2
_MAX_REPLICAS = 12
_CPU_REQUEST = "250m"
_MEMORY_REQUEST = "512Mi"
_CPU_LIMIT = "1"
_MEMORY_LIMIT = "1Gi"


class Apps(Seed):
    """Deploy app instances, their consumed files, their routing and their dials."""

    def seed_source_apps(self, source: str = SEED_SOURCE_NAME) -> bool:
        """Deploy the transform and fetcher bound to *source*, with a program to edit.

        Returns True when anything was written, False when it was all already there.
        """
        changed = self._ensure_instance(TRANSFORM_SERVICE, source)
        changed |= self._ensure_transform_program(source)
        changed |= self._ensure_instance(FETCHER_SERVICE, source)
        return changed

    def seed_pools(self) -> bool:
        """Deploy the single-instance pools and compile their routing from the sources.

        Returns True when anything was written, False when it was all already there.
        """
        changed = False
        for service in POOL_SERVICES:
            changed |= self._ensure_instance(service, POOL_INSTANCE)
            changed |= self._ensure_routing(service, POOL_INSTANCE)
        return changed

    def seed_app_scaling_state(self) -> bool:
        """Set non-default scaling dials on every pool.

        Returns True when anything was written, False when it was all already there.
        """
        changed = False
        for service in POOL_SERVICES:
            changed |= self._ensure_instance(service, POOL_INSTANCE)
            changed |= self._ensure_dials(service, POOL_INSTANCE)
        return changed

    def delete_all(self) -> None:
        """Undeploy every managed instance. A no-op without a deploy repo."""
        gc = self._gitcrud
        if gc is None:
            return
        for app in instances.list_instances(gc):
            gc.delete(
                instances.HELMVARS_CLASS,
                app.overlay_name,
                SEED_ACTOR,
                message=f"e2e: undeploy {app.service}/{app.instance}",
            )

    def delete_run_state(self) -> None:
        """Clear the deploy-repo classes no seeder creates but a spec can change.

        Nothing seeds either of these at startup and both are only ever written
        through an API a spec drives, so whatever is there came from the run that
        just finished. Deleting the resource restores the shipped default rather
        than removing configuration: an absent overlay declares nothing, and an
        absent auto-merge flag reads as off.

        Without this, a backing-service node count raised by one spec is still
        raised for the next -- the state that survived a reset and failed a spec
        against its own leftovers.
        """
        gc = self._gitcrud
        if gc is None:
            return
        for cls_name in RUN_STATE_CLASSES:
            for name in gc.list(cls_name):
                gc.delete(
                    cls_name,
                    name,
                    SEED_ACTOR,
                    message=f"e2e: clear {cls_name}/{name}",
                )

    def _ensure_instance(self, service: str, instance: str) -> bool:
        """Create the instance's overlay when absent. Returns whether it was written."""
        gc = self._require_gitcrud()
        app = instances.instance_of(service, instance)
        if instances.exists(gc, app):
            return False
        return self._put(app, instances.initial_overlay(app), "deploy instance")

    def _ensure_transform_program(self, source: str) -> bool:
        """Write the seeded program into the transform's file set."""
        gc = self._require_gitcrud()
        app = instances.instance_of(TRANSFORM_SERVICE, source)
        file_set = catalogue.file_set(TRANSFORM_SERVICE, TRANSFORM_FILE_SET)
        doc = instances.read_overlay(gc, app)
        if not files.upsert_file(doc, file_set, _TRANSFORM_FILENAME, _TRANSFORM_PROGRAM):
            return False
        return self._put(app, doc, f"set {_TRANSFORM_FILENAME}")

    def _ensure_routing(self, service: str, instance: str) -> bool:
        """Rewrite the overlay's routing to what the sources currently compile to."""
        gc = self._require_gitcrud()
        app = instances.instance_of(service, instance)
        doc = instances.read_overlay(gc, app)
        if not routing.sync(
            app.descriptor, doc, self._require_source_registry(), self._require_settings()
        ):
            return False
        return self._put(app, doc, "sync routing")

    def _ensure_dials(self, service: str, instance: str) -> bool:
        """Set the scaling dials, whatever the deploy target reports about them.

        The dials are overlay values; whether they APPLY is a deploy-target
        question the API answers on read, so a seed writes them either way.
        """
        gc = self._require_gitcrud()
        app = instances.instance_of(service, instance)
        doc = instances.read_overlay(gc, app)
        changes = scaling.changes(
            doc,
            min_replicas=_MIN_REPLICAS,
            max_replicas=_MAX_REPLICAS,
            keda_enabled=True,
            cpu_request=_CPU_REQUEST,
            memory_request=_MEMORY_REQUEST,
            cpu_limit=_CPU_LIMIT,
            memory_limit=_MEMORY_LIMIT,
        )
        for path, value in changes.items():
            set_path(doc, path, value)
        return self._put(app, doc, "set scaling dials")

    def _put(self, app: instances.AppInstance, doc: dict, summary: str) -> bool:
        """Commit one overlay. Returns whether the commit carried a change."""
        gc = self._require_gitcrud()
        result = gc.put(
            instances.HELMVARS_CLASS,
            app.overlay_name,
            doc,
            SEED_ACTOR,
            message=f"e2e({app.service}/{app.instance}): {summary}",
        )
        return bool(result.changed)
