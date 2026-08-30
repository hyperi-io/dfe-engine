#  Project:      dfe-engine
#  File:         api/e2e/seed/artefacts.py
#  Purpose:      Library-artefact seeder primitives for e2e-server
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Library seeding. Public methods are the scripts; privates are reusable.

An artefact is a gitcrud bundle, so its manifest and its version files land in one
commit through ``GitCrud.put_bundle`` -- the same path ``api/v1/library.py`` writes
through. The seeded artefact carries two versions, a tag on the earlier one and a
label map, and one instance file links to that earlier version, so the Library page,
the usage list and the per-file link surface all have something to render. The link
is pinned to a version behind the current one deliberately: that is what makes the
``outdated`` signal visible without inventing drift.
"""

from __future__ import annotations

from dfe_engine.api.e2e.seed.apps import TRANSFORM_FILE_SET, TRANSFORM_SERVICE
from dfe_engine.api.e2e.seed.base import Seed
from dfe_engine.api.e2e.seed.sources import SEED_ACTOR, SEED_SOURCE_NAME
from dfe_engine.appmgmt import catalogue, instances, library, links
from dfe_engine.gitcrud.engine import ResourceNotFoundError

SEED_ARTIFACT_NAME = "seed-artefact"
SEED_ARTIFACT_KIND = "vrl"
SEED_ARTIFACT_TAG = "stable"
SEED_LINKED_FILENAME = "seed_library.vrl"

_ARTIFACT_GROUP = "seed"
_ARTIFACT_DESCRIPTION = "Playwright fixture artefact."
_ARTIFACT_LABELS = {"team": "platform", "tier": "gold"}

_TAGGED_VERSION = 1
_V1 = "# Seeded artefact, version 1.\n.dfe_artefact = 1\n"
_V2 = "# Seeded artefact, version 2.\n.dfe_artefact = 2\n.dfe_enriched = true\n"


class Artefacts(Seed):
    """Create or reset artefacts in the versioned library, and link one into an app."""

    def seed_library_artefact(
        self,
        name: str = SEED_ARTIFACT_NAME,
        *,
        instance: str = SEED_SOURCE_NAME,
    ) -> bool:
        """Seed a two-version artefact with a tag, labels and one instance link.

        Returns True when anything was written, False when it was all already there.
        """
        changed = self._ensure_artifact(name)
        changed |= self._ensure_version(name, version=1, content=_V1)
        changed |= self._ensure_version(name, version=2, content=_V2)
        changed |= self._ensure_tag(name, SEED_ARTIFACT_TAG, _TAGGED_VERSION)
        changed |= self._ensure_link(name, instance, version=_TAGGED_VERSION)
        return changed

    def delete_all(self) -> None:
        """Clear every library artefact. A no-op without a deploy repo."""
        gc = self._gitcrud
        if gc is None:
            return
        for name in gc.list(links.LIBRARY_CLASS):
            gc.delete(
                links.LIBRARY_CLASS,
                name,
                SEED_ACTOR,
                message=f"e2e: remove artefact {name}",
            )

    def _ensure_artifact(self, name: str) -> bool:
        """Create the artefact when absent, else reset its classification metadata."""
        gc = self._require_gitcrud()
        try:
            doc = gc.get(links.LIBRARY_CLASS, name)
        except ResourceNotFoundError:
            doc = library.new_artifact(
                library.kind(SEED_ARTIFACT_KIND),
                group=_ARTIFACT_GROUP,
                description=_ARTIFACT_DESCRIPTION,
                labels=dict(_ARTIFACT_LABELS),
            )
            return self._put(name, doc, "create artefact")
        if not library.set_metadata(
            doc,
            description=_ARTIFACT_DESCRIPTION,
            labels=dict(_ARTIFACT_LABELS),
            group=_ARTIFACT_GROUP,
        ):
            return False
        return self._put(name, doc, "edit metadata")

    def _ensure_version(self, name: str, *, version: int, content: str) -> bool:
        """Publish content at an exact version.

        The version is explicit so a second call recognises the content already
        published there instead of appending it to the series again.
        """
        gc = self._require_gitcrud()
        doc = gc.get(links.LIBRARY_CLASS, name)
        published, changed, plan = library.publish(
            doc,
            content=content,
            actor=SEED_ACTOR,
            description=f"seeded version {version}",
            version=version,
        )
        if not changed:
            return False
        return self._put(name, doc, f"publish v{published}", plan=plan)

    def _ensure_tag(self, name: str, tag: str, version: int) -> bool:
        """Point a tag at a version."""
        gc = self._require_gitcrud()
        doc = gc.get(links.LIBRARY_CLASS, name)
        if not library.set_tag(doc, tag, version):
            return False
        return self._put(name, doc, f"tag {tag} v{version}")

    def _ensure_link(self, name: str, instance: str, *, version: int) -> bool:
        """Resolve the artefact into the transform's file set and record the link."""
        gc = self._require_gitcrud()
        app = instances.instance_of(TRANSFORM_SERVICE, instance)
        file_set = catalogue.file_set(TRANSFORM_SERVICE, TRANSFORM_FILE_SET)
        doc = instances.read_overlay(gc, app)
        if not links.resolve(
            doc,
            file_set,
            name=SEED_LINKED_FILENAME,
            artifact=name,
            env=gc.get(links.LIBRARY_CLASS, name),
            source=links.crud_source(gc),
            version=version,
        ):
            return False
        result = gc.put(
            instances.HELMVARS_CLASS,
            app.overlay_name,
            doc,
            SEED_ACTOR,
            message=f"e2e({app.service}/{app.instance}): link {SEED_LINKED_FILENAME}",
        )
        return bool(result.changed)

    def _put(
        self,
        name: str,
        doc: dict,
        summary: str,
        *,
        plan: library.BundlePlan | None = None,
    ) -> bool:
        """Commit one artefact bundle. Returns whether the commit carried a change."""
        gc = self._require_gitcrud()
        result = gc.put_bundle(
            links.LIBRARY_CLASS,
            name,
            doc,
            SEED_ACTOR,
            writes=dict((plan or library.BundlePlan()).writes),
            removals=list((plan or library.BundlePlan()).removals),
            message=f"e2e({name}): {summary}",
        )
        return bool(result.changed)
