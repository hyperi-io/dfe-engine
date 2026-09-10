#  Project:      dfe-engine
#  File:         appmgmt/seed.py
#  Purpose:      Seed the versioned library from the content a deployment mounts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Reference files another repo ships, published into the library at startup.

The transforms ship reference pipelines -- a runnable Vector topology, a filebeat
VRL program -- and a deployment materialises them into a directory from a version
pin (dfe-infra ``versions.yaml`` ``content:``, mounted by the engine chart). This
turns that directory into library artefacts, so the console shows them beside
everything a person authored and a link resolves against them like any other.

The directory is laid out by kind, because the kind is what says how a file is
stored and validated and the mount is the only place it can come from::

    <DFE_LIBRARY_SEED_DIR>/vrl/filebeat.vrl        -> artefact `filebeat`
    <DFE_LIBRARY_SEED_DIR>/vector-yaml/bus.yaml    -> artefact `bus`

Idempotent by construction, because it runs on every boot and a deployment
restarts for reasons that have nothing to do with content: an artefact is created
only when absent, content is published only when NO version in the series already
holds its digest, and the ``stack`` tag then points at whichever version carries
it. Matching across the whole series rather than against ``current`` is what
leaves an operator's own later version alone, and what makes a rolled-back pin
move the tag rather than append a copy of content the artefact already has.

What is seeded is marked as such: group ``stack`` and the label
``origin=stack``, so the console can tell shipped content from authored content
and an operator can list one without the other.
"""

from __future__ import annotations

from pathlib import Path

from scalo.logger import logger

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message, validate_name
from dfe_engine.gitcrud.engine import ResourceNotFoundError
from dfe_engine.manifest import manifest_path

from . import library
from .links import LIBRARY_CLASS

SEED_DIR_ENV = "DFE_LIBRARY_SEED_DIR"
"""Where a deployment mounts the content it wants published. Unset seeds nothing.

No conventional fallback, unlike the manifests: this directory is filled by an
init container the same chart sets the variable in, so an unset variable means a
deployment that materialises no content rather than one that forgot the path.
"""

SEED_ACTOR = "dfe-engine"
"""The engine seeds as itself: this is derived state, not somebody's edit."""

SEED_GROUP = "stack"
"""Namespace every seeded artefact lands in, so shipped content lists apart."""

SEED_TAG = "stack"
"""Mutable pointer at whatever version the pin currently carries."""

SEED_ORIGIN_LABEL = {"origin": "stack"}
"""What marks an artefact as shipped rather than authored."""


def _description(kind_name: str, name: str) -> str:
    return f"Reference {kind_name} shipped with the stack: {name}"


def _files(seed_dir: Path) -> list[tuple[library.ArtifactKind, Path]]:
    """Every seedable file under the mount, as (kind, path), in a stable order.

    A subdirectory that is not a declared kind, and a file whose suffix that kind
    does not claim, are both skipped with a line rather than refused: the mount is
    filled by whatever the pins carry, and one unexpected file must not stop the
    rest from reaching the library.
    """
    found: list[tuple[library.ArtifactKind, Path]] = []
    for kind_dir in sorted(p for p in seed_dir.iterdir() if p.is_dir()):
        try:
            artifact_kind = library.kind(kind_dir.name)
        except library.UnknownKindError:
            logger.info(
                "Skipped seed content under an undeclared kind",
                directory=str(kind_dir),
                kind=kind_dir.name,
            )
            continue
        for path in sorted(p for p in kind_dir.iterdir() if p.is_file()):
            if path.suffix not in artifact_kind.suffixes:
                logger.info(
                    "Skipped seed content whose suffix its kind does not claim",
                    file=str(path),
                    kind=artifact_kind.name,
                )
                continue
            found.append((artifact_kind, path))
    return found


def _read(artifact_kind: library.ArtifactKind, path: Path) -> str:
    """The file in the text form ``library.publish`` takes for this kind."""
    return library.as_text(artifact_kind, path.read_bytes())


def _create(gc: GitCrud, name: str, artifact_kind: library.ArtifactKind) -> dict:
    """The artefact's envelope, created when this deployment has none."""
    try:
        return gc.get(LIBRARY_CLASS, name)
    except ResourceNotFoundError:
        pass
    doc = library.new_artifact(
        artifact_kind,
        group=SEED_GROUP,
        description=_description(artifact_kind.name, name),
        labels=dict(SEED_ORIGIN_LABEL),
    )
    _commit(gc, name, doc, "create shipped artefact")
    return doc


def _commit(
    gc: GitCrud,
    name: str,
    doc: dict,
    summary: str,
    plan: library.BundlePlan | None = None,
) -> None:
    """One artefact bundle, manifest and content files in the same commit."""
    bundle = plan or library.BundlePlan()
    gc.put_bundle(
        LIBRARY_CLASS,
        name,
        doc,
        SEED_ACTOR,
        writes=dict(bundle.writes),
        removals=list(bundle.removals),
        message=build_message(
            CommitContext(
                ctype="seed",
                scope=name,
                summary=summary,
                actor=SEED_ACTOR,
                role="library:write",
            )
        ),
    )


def seed_dir() -> Path | None:
    """The mounted content directory, or None when this deployment mounts none."""
    return manifest_path(None, SEED_DIR_ENV, None)


def seed_library(gc: GitCrud, directory: Path | str | None) -> list[str]:
    """Publish the mounted content into the library. Returns what changed.

    An absent, unnamed or empty directory is a no-op: most deployments mount no
    content, and the engine offers none rather than treating that as a fault.
    """
    if not directory:
        logger.info("No library seed directory configured", env=SEED_DIR_ENV)
        return []
    root = Path(directory)
    if not root.is_dir():
        logger.info("No library seed content mounted", directory=str(root))
        return []

    files = _files(root)
    if not files:
        logger.info("Library seed directory carries no content", directory=str(root))
        return []

    changed: list[str] = []
    for artifact_kind, path in files:
        try:
            changed += _seed_one(gc, artifact_kind, path)
        except (ValueError, KeyError) as exc:
            # One unusable file must not cost the rest their seeding, and the
            # deployment cannot fix it here -- the pin is what carries it.
            logger.warning("Seed content could not be published", file=str(path), error=str(exc))
    return changed


def _seed_one(gc: GitCrud, artifact_kind: library.ArtifactKind, path: Path) -> list[str]:
    """Bring one file's artefact up to date. Returns what changed, if anything."""
    name = path.stem
    validate_name(name)
    content = _read(artifact_kind, path)
    doc = _create(gc, name, artifact_kind)

    # Match on DIGEST across the whole series, not just the version `current`
    # names: a person who published their own version over a seeded artefact
    # keeps it as current, and unchanged shipped content is republished nowhere.
    digest = library.digest_of(artifact_kind, content)
    held = next((v.version for v in library.versions(doc) if v.digest == digest), None)

    changed: list[str] = []
    if held is None:
        held, _, plan = library.publish(
            doc,
            content=content,
            actor=SEED_ACTOR,
            description=_description(artifact_kind.name, name),
            message=f"shipped content from {path.name}",
        )
        _commit(gc, name, doc, f"publish v{held}", plan=plan)
        changed.append(f"{name}: published v{held}")

    # The tag follows the version carrying the pinned content, published now or
    # earlier, so a rolled-back pin brings the pointer back with it.
    if library.set_tag(doc, SEED_TAG, held):
        _commit(gc, name, doc, f"tag {SEED_TAG} v{held}")
        changed.append(f"{name}: {SEED_TAG} -> v{held}")
    return changed
