#  Project:      dfe-engine
#  File:         appmgmt/links.py
#  Purpose:      Links from an instance's consumed files to library artefacts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An instance's file set can be authored in place, or linked to the library.

Helm cannot dereference a link, so the overlay carries BOTH: the resolved content
in the file set's ``values_path``, which is what a chart renders, and the link
provenance in its ``links_path``, which is what says where that content came from.
The link is the intent; the resolved content is the build output committed beside
it.

That pair is what makes three things possible. Rolling back is repointing the link
and re-resolving. Fixing once and rolling everywhere is publishing a new artefact
version and re-resolving the instances that link to it. And drift is detectable,
because content whose digest no longer matches the version it claims to have come
from is visible rather than silently divergent.

A link may name a version number or a tag. Either way the RESOLVED version and
digest are recorded, so what deployed is never ambiguous; re-resolving a tag link
is what picks up a repointed tag.

These are pure document operations. Committing them is the router's job, so the
write goes through the same policy and review routing as every other overlay change.
"""

from collections.abc import Callable
from dataclasses import dataclass

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.engine import ResourceNotFoundError, get_path, set_path
from dfe_engine.yaml_utils import literal_block

from . import files, library
from .catalogue import ConsumedFileSet, descriptor
from .instances import HELMVARS_CLASS, AppInstance, list_instances

LIBRARY_CLASS = "library"

_NAME = "name"
_ARTIFACT = "artifact"
_VERSION = "version"
_DIGEST = "digest"
_TAG = "tag"


class LinkNotFoundError(KeyError):
    """Raised when the named file in the set carries no link."""


class ArtifactNotLinkableError(ValueError):
    """Raised when an artefact cannot be linked into this file set."""


@dataclass(frozen=True, slots=True)
class Link:
    """Where one file in a set came from."""

    name: str
    artifact: str
    version: int
    digest: str
    tag: str = ""
    """The tag the link was made through, empty when it pins a version number."""


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """A link, checked against the library and against the content beside it."""

    link: Link
    resolved_digest: str = ""
    """Digest of the content actually sitting in the file set."""

    available_version: int | None = None
    """The version re-resolving would move this link to."""

    missing: bool = False
    """The artefact or its linked version is gone from the library."""

    drift: bool = False
    """The content in the file set is not what the linked version holds."""

    outdated: bool = False
    """The link's target has moved on since it was resolved."""


@dataclass(frozen=True, slots=True)
class LibrarySource:
    """How links read the library: a manifest by name, and a version's content.

    Content lives in a file beside the manifest rather than inside it, so reading
    a version is a second, explicit lookup instead of a field access.
    """

    envelope: Callable[[str], dict | None]
    """Reads an artefact manifest by name, or returns None when it is absent."""

    content: Callable[[str, str], str]
    """Reads one bundle-relative path from an artefact, as overlay-ready text."""


def crud_source(gc: GitCrud) -> LibrarySource:
    """A library source reading artefacts out of the deploy repo."""

    def _envelope(name: str) -> dict | None:
        try:
            return gc.get(LIBRARY_CLASS, name)
        except ResourceNotFoundError, ValueError:
            return None

    def _content(name: str, path: str) -> str:
        env = _envelope(name)
        if env is None:
            raise ResourceNotFoundError(name)
        raw = gc.read_payload_bytes(LIBRARY_CLASS, name, path)
        return library.as_text(library.artifact_kind_of(env), raw)

    return LibrarySource(envelope=_envelope, content=_content)


def _entries(doc: dict, file_set: ConsumedFileSet) -> list[dict]:
    raw = get_path(doc, file_set.links_path, default=None)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"{file_set.links_path} holds {type(raw).__name__}, expected a list of link entries"
        )
    for entry in raw:
        if not isinstance(entry, dict) or _NAME not in entry:
            raise ValueError(
                f"{file_set.links_path} holds an entry without a {_NAME!r} key; "
                "repair the overlay before editing its links"
            )
    return list(raw)


def list_links(doc: dict, file_set: ConsumedFileSet) -> list[Link]:
    """Every link in the set, in the order the overlay stores them."""
    return [
        Link(
            name=str(entry[_NAME]),
            artifact=str(entry.get(_ARTIFACT, "")),
            version=int(entry.get(_VERSION, 0)),
            digest=str(entry.get(_DIGEST, "")),
            tag=str(entry.get(_TAG, "")),
        )
        for entry in _entries(doc, file_set)
    ]


def read_link(doc: dict, file_set: ConsumedFileSet, name: str) -> Link:
    """The link behind one file in the set."""
    for candidate in list_links(doc, file_set):
        if candidate.name == name:
            return candidate
    raise LinkNotFoundError(name)


def _store(doc: dict, file_set: ConsumedFileSet, link: Link) -> None:
    entry: dict = {
        _NAME: link.name,
        _ARTIFACT: link.artifact,
        _VERSION: link.version,
        _DIGEST: link.digest,
    }
    # Written only when the link was made through a tag, so a version-pinned link
    # stays a four-field entry in the committed YAML.
    if link.tag:
        entry[_TAG] = link.tag
    entries = _entries(doc, file_set)
    for i, existing in enumerate(entries):
        if existing[_NAME] == link.name:
            entries[i] = entry
            set_path(doc, file_set.links_path, entries)
            return
    entries.append(entry)
    set_path(doc, file_set.links_path, entries)


def remove_link(doc: dict, file_set: ConsumedFileSet, name: str) -> bool:
    """Drop a link, leaving the resolved content in place. Mutates ``doc``."""
    entries = _entries(doc, file_set)
    remaining = [e for e in entries if e[_NAME] != name]
    if len(remaining) == len(entries):
        raise LinkNotFoundError(name)
    set_path(doc, file_set.links_path, remaining)
    return True


def check_linkable(file_set: ConsumedFileSet, env: dict, name: str) -> None:
    """Refuse an artefact this file set could not actually consume.

    The filename has to be one the app reads AND one its kind may carry, or the
    chart would mount a file the app either ignores or cannot parse. A disabled
    artefact is refused outright.
    """
    if library.state_of(env) is library.LifecycleState.DISABLED:
        raise ArtifactNotLinkableError("artefact is disabled")
    files.validate_filename(file_set, name)
    artifact_kind = library.artifact_kind_of(env)
    if not name.endswith(artifact_kind.suffixes):
        allowed = ", ".join(artifact_kind.suffixes)
        raise ArtifactNotLinkableError(
            f"{name!r} does not end in an extension a {artifact_kind.name} artefact "
            f"may carry (expected: {allowed})"
        )


def resolve(
    doc: dict,
    file_set: ConsumedFileSet,
    *,
    name: str,
    artifact: str,
    env: dict,
    source: LibrarySource,
    version: int | None = None,
    tag: str = "",
) -> bool:
    """Link a file in the set to an artefact version. Mutates ``doc``.

    Resolving writes the version's content into the file set and records the
    provenance beside it. Returns whether either half changed.
    """
    check_linkable(file_set, env, name)
    if tag:
        target = library.resolve_tag(env, tag)
    elif version is not None:
        target = int(version)
    else:
        current = env.get("current")
        if current is None:
            raise ArtifactNotLinkableError(f"{artifact} has no published version")
        target = int(current)

    found = library.read_version(env, target)
    link = Link(name=name, artifact=artifact, version=target, digest=found.digest, tag=tag)
    before = _entries(doc, file_set)
    content_changed = files.upsert_file(doc, file_set, name, source.content(artifact, found.path))
    _store(doc, file_set, link)
    return content_changed or _entries(doc, file_set) != before


def relink(doc: dict, file_set: ConsumedFileSet, source: LibrarySource) -> list[Link]:
    """Re-resolve every link in the set. Mutates ``doc``. Returns the links moved.

    A tag link follows its tag; a version-pinned link advances to the artefact's
    current version. An artefact that is gone or disabled is left alone, so a
    re-resolve never silently drops content an instance is running.
    """
    moved: list[Link] = []
    for link in list_links(doc, file_set):
        env = source.envelope(link.artifact)
        if env is None or library.state_of(env) is library.LifecycleState.DISABLED:
            continue
        try:
            target = library.resolve_tag(env, link.tag) if link.tag else _current_of(env)
        except library.TagNotFoundError, library.VersionNotFoundError:
            continue
        if target is None:
            continue
        try:
            found = library.read_version(env, target)
            content = source.content(link.artifact, found.path)
        except library.VersionNotFoundError, ResourceNotFoundError:
            continue
        # The content alone decides: upsert_file also reports a table entry it named.
        moved_here = _content_of(doc, file_set, link.name) != str(literal_block(content))
        files.upsert_file(doc, file_set, link.name, content)
        if target != link.version or found.digest != link.digest:
            moved_here = True
        _store(
            doc,
            file_set,
            Link(
                name=link.name,
                artifact=link.artifact,
                version=target,
                digest=found.digest,
                tag=link.tag,
            ),
        )
        if moved_here:
            moved.append(read_link(doc, file_set, link.name))
    return moved


def _content_of(doc: dict, file_set: ConsumedFileSet, name: str) -> str | None:
    """The content the set holds under ``name``, or None when it holds no such file."""
    try:
        return files.read_file(doc, file_set, name).content
    except files.FileNotInSetError:
        return None


def _current_of(env: dict) -> int | None:
    current = env.get("current")
    return None if current is None else int(current)


def status(doc: dict, file_set: ConsumedFileSet, source: LibrarySource) -> list[LinkStatus]:
    """Check every link against the library and against the content beside it."""
    out: list[LinkStatus] = []
    for link in list_links(doc, file_set):
        env = source.envelope(link.artifact)
        if env is None:
            out.append(LinkStatus(link=link, missing=True))
            continue
        try:
            found = library.read_version(env, link.version)
            artifact_kind = library.artifact_kind_of(env)
        except library.VersionNotFoundError, library.UnknownKindError:
            out.append(LinkStatus(link=link, missing=True))
            continue
        try:
            resolved = files.read_file(doc, file_set, link.name).content
            resolved_digest = library.digest_of(artifact_kind, resolved)
        except files.FileNotInSetError, library.InvalidArtifactError:
            resolved_digest = ""
        available = library.tags_of(env).get(link.tag) if link.tag else _current_of(env)
        out.append(
            LinkStatus(
                link=link,
                resolved_digest=resolved_digest,
                available_version=available,
                drift=resolved_digest != found.digest,
                outdated=available is not None and available != link.version,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Usage:
    """One place an artefact is linked."""

    service: str
    instance: str
    file_set: str
    name: str
    version: int
    tag: str = ""


def usage(gc: GitCrud, artifact: str) -> list[Usage]:
    """Every instance file that links to this artefact.

    Read straight off the overlays rather than from an index, because the deploy
    repo is the record and an index beside it would be a second thing to drift.
    """
    out: list[Usage] = []
    for app in list_instances(gc):
        for file_set in descriptor(app.service).files:
            for link in _links_of(gc, app, file_set):
                if link.artifact == artifact:
                    out.append(
                        Usage(
                            service=app.service,
                            instance=app.instance,
                            file_set=file_set.name,
                            name=link.name,
                            version=link.version,
                            tag=link.tag,
                        )
                    )
    return out


def _links_of(gc: GitCrud, app: AppInstance, file_set: ConsumedFileSet) -> list[Link]:
    """One instance's links, treating an unreadable overlay as carrying none."""
    try:
        return list_links(gc.get(HELMVARS_CLASS, app.overlay_name), file_set)
    except ResourceNotFoundError, ValueError:
        return []
