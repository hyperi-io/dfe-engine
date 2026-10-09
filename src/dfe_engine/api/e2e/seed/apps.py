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

from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import (
    SEED_ACTOR,
    SEED_FETCHER_SOURCE_NAME,
    SEED_SOURCE_NAME,
)
from dfe_engine.appmgmt import (
    catalogue,
    derived,
    files,
    instances,
    routing,
    scaling,
    seed_instances,
)
from dfe_engine.gitcrud.engine import get_path, set_path

TRANSFORM_SERVICE = "dfe-transform-vrl"
"""The transform an instance of which IS a source's processing step."""

TRANSFORM_ENGINES = (
    ("vrl", "dfe-transform-vrl", None),
    ("vector", "dfe-transform-vector", None),
    ("elastic", "dfe-transform-elastic", "filebeat.cisco_ios.default"),
)
"""Engine name, its catalogued app, and the compiled-in program where it selects one.

dfe-transform-elastic reads no authored files and picks its program by name, so it
carries a variant while the two file-driven apps take None.
"""

TRANSFORM_FILE_SET = "transforms"
"""The file set the seeded program and the seeded library link land in."""

FETCHER_SERVICE = "dfe-fetcher"
POOL_SERVICES = ("dfe-receiver", "dfe-loader")
POOL_INSTANCE = "default"
"""Instance name for a single-multiplicity app: one deployment for the whole stack."""

MANAGED_SERVICES = frozenset(
    {FETCHER_SERVICE, *POOL_SERVICES, *(s for _e, s, _v in TRANSFORM_ENGINES)}
)
"""Apps a seed method here can deploy, so a reset touches only these.

dfe-engine, dfe-ui, hyperdx and culvert also catalogue a values overlay, but no
seeder writes one: it is standing infra a deployment carries from the moment it
is stood up, not per-run state this class created. ``list_instances`` returns
every catalogued app's overlay, so the filter is what keeps a reset from pruning
one of theirs along with the sources and pools this class actually seeds.
"""

_DEPLOY_BLOCK = catalogue.DEPLOY_SERVICE_PATH.rpartition(".")[0]
"""The overlay block the ApplicationSet names an Application from, which a pool keeps whole."""

RUN_STATE_CLASSES = ("infravars", "gov_settings")
"""Deploy-repo classes a reset clears, beyond the ones the child seeders own.

Both are per-run state on the same three tests: nothing seeds them at startup,
each is only written through an API a spec drives, and its absence IS the shipped
default. ``infravars`` is the infra overlay (backing-service node counts,
storage, CPU); ``gov_settings`` is the auto-merge posture flag.

Deliberately NOT here: ``actions`` and ``policies`` are the shipped governance
library, re-seeded from packaged resources on every start; ``accounts`` is the
durable break-glass copy the reset re-mirrors rather than removes.
"""

_COMMON_OVERLAY = "common"
"""``infravars``' deployment-wide file (api/v1/backing_services.py ``_COMMON``).

Read before every per-chart overlay, so it is the deployer's own baseline for
node counts, storage and CPU rather than state a spec wrote -- on Kubernetes it
predates any e2e run. A per-chart name under the same class is still cleared.
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

    def seed_source_apps(
        self, source: str = SEED_SOURCE_NAME, fetched: str = SEED_FETCHER_SOURCE_NAME
    ) -> bool:
        """Deploy the transform bound to *source*, with a program to edit, and the
        fetcher instance bound to *fetched*.

        Returns True when anything was written, False when it was all already there.
        """
        changed = self._ensure_instance(TRANSFORM_SERVICE, source)
        changed |= self._ensure_transform_program(source)
        changed |= self._ensure_fetcher_instance(fetched)
        return changed

    def seed_transform_instances(self, sources: dict[str, str]) -> bool:
        """Deploy one transform instance per entry, mapping source name to its app.

        Returns True when anything was written, False when it was all already there.
        """
        changed = False
        for source, service in sources.items():
            changed |= self._ensure_instance(service, source)
        return changed

    def _ensure_fetcher_instance(self, source: str) -> bool:
        """Deploy the fetcher instance for *source* with its stanza compiled in."""
        gc = self._require_gitcrud()
        app = instances.instance_of(FETCHER_SERVICE, source)
        doc = instances.read_overlay(gc, app) if instances.exists(gc, app) else None
        fresh = doc is None
        doc = doc or instances.initial_overlay(app)
        synced = routing.sync(
            app.descriptor,
            doc,
            self._require_source_registry(),
            self._require_settings(),
            instance=source,
        )
        if not (fresh or synced):
            return False
        return self._put(app, doc, "deploy instance" if fresh else "sync routing")

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
        """Undeploy every per-source instance a seed method here can create.

        A no-op without a deploy repo. Restricted to ``MANAGED_SERVICES``, and the
        pools are left to ``reset_pools``: dfe-engine, dfe-ui, hyperdx and culvert
        also catalogue a values overlay, but no seeder writes one, so deleting
        theirs would prune standing infra rather than reset per-run state.
        """
        gc = self._gitcrud
        if gc is None:
            return
        for app in instances.list_instances(gc):
            if app.service not in MANAGED_SERVICES or app.service in POOL_SERVICES:
                continue
            gc.delete(
                instances.HELMVARS_CLASS,
                app.overlay_name,
                SEED_ACTOR,
                message=f"e2e: undeploy {app.service}/{app.instance}",
            )

    def reset_pools(self) -> None:
        """Put each pool back to what the deployment was stood up with.

        A no-op without a deploy repo. A pool the deployer's marker names is the
        deployment's own data path -- on Kubernetes, deleting its overlay makes Argo
        prune the Application -- so it is rewritten to the identity the deployer gave
        it, and recreated if a run removed it. A pool the marker does not name was
        created by a seed here, so it goes. The routing a pool carries is derived from
        the sources, and ``reconcile_derived`` compiles it back in once they are reset.
        """
        gc = self._gitcrud
        if gc is None:
            return
        offered = seed_instances.marked_offered(gc)
        for service in POOL_SERVICES:
            app = instances.instance_of(service, POOL_INSTANCE)
            present = instances.exists(gc, app)
            if service not in offered:
                if present:
                    gc.delete(
                        instances.HELMVARS_CLASS,
                        app.overlay_name,
                        SEED_ACTOR,
                        message=f"e2e: undeploy {app.service}/{app.instance}",
                    )
                    self._metrics.pool_reset(service, "removed")
                continue
            doc = instances.read_overlay(gc, app) if present else None
            self._put(app, _baseline(app, doc), "restore deployed baseline")
            self._metrics.pool_reset(service, "restored" if present else "recreated")

    def reconcile_derived(self) -> list[str]:
        """Recompile every app's derived state from the sources, as the engine does at start.

        A no-op without a deploy repo or a source registry. The receiver refuses to
        start without a destination, so a reset recompiles a pool's routing before
        its commit lands.
        """
        gc = self._gitcrud
        registry = self._source_registry
        if gc is None or registry is None:
            return []
        return derived.reconcile(gc, registry, self._require_settings(), actor=SEED_ACTOR)

    def delete_run_state(self) -> None:
        """Clear the deploy-repo classes no seeder creates but a spec can change.

        Nothing seeds either of these at startup and both are only ever written
        through an API a spec drives, so whatever is there came from the run that
        just finished. Deleting the resource restores the shipped default rather
        than removing configuration: an absent overlay declares nothing, and an
        absent auto-merge flag reads as off.

        Without this, a backing-service node count raised by one spec is still
        raised for the next -- the state that survived a reset and failed a spec
        against its own leftovers. The deployer's own ``_COMMON_OVERLAY`` is the
        one exception: it is not per-run state, so it is left in place.
        """
        gc = self._gitcrud
        if gc is None:
            return
        for cls_name in RUN_STATE_CLASSES:
            for name in gc.list(cls_name):
                if cls_name == "infravars" and name == _COMMON_OVERLAY:
                    continue
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


def _baseline(app: instances.AppInstance, doc: dict | None) -> dict:
    """The pool overlay as its deployer wrote it: the identity, and nothing a run added.

    Both deployers write identity alone -- the Kubernetes setup job the ``deploy``
    block, the engine on Compose ``instances.initial_overlay`` -- so the identity
    paths are kept from *doc* where it has them and everything else (routing, dials,
    files, links) goes. A pool with no overlay left gets the engine's own.
    """
    initial = instances.initial_overlay(app)
    if doc is None:
        return initial
    baseline: dict = {_DEPLOY_BLOCK: dict(doc.get(_DEPLOY_BLOCK) or initial[_DEPLOY_BLOCK])}
    identity = [
        catalogue.OTEL_SERVICE_NAME_PATH,
        catalogue.DFE_COMMON_OTEL_SERVICE_NAME_PATH,
        catalogue.FULLNAME_OVERRIDE_PATH,
        catalogue.DFE_COMMON_COMPONENT_PATH,
        *catalogue.render_source_binding(app.descriptor, app.instance),
    ]
    for path in identity:
        value = get_path(doc, path)
        if value is not None:
            set_path(baseline, path, value)
    return baseline
