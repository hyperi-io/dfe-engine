#  Project:      dfe-engine
#  File:         appmgmt/library.py
#  Purpose:      The versioned artefact library - immutable content, mutable pointers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A versioned library of authored files, generic over what the file IS.

Anything needing an authored file under version control uses this; a transform is
only its first consumer. An artefact is a named series of immutable versions plus
mutable metadata, and its ``kind`` says what sort of file it holds. Kinds are
declared in the app manifest, so supporting a new authored language is an edit
there rather than a change here.

Three things are deliberately separate:

- **Versions** are content, immutable once published. A version carries the
  content, its digest, its encoding, the kind it was published under, a
  description fixed at publish time, and when it was published.
- **Labels** are classification - a free-text description, a flat string map, and
  an optional group namespace. They are artefact-level and editable at any time,
  and editing them never produces a version.
- **Tags** are mutable named pointers to a version, so a caller can pin or roll
  back by name instead of by number. Repointing a tag is its own operation, never
  a side effect of publishing.

The versioning envelope is ``VersionedDoc``'s - ``{current, deployed, status,
draft, versions}`` with monotonic integer versions and immutable published
snapshots. These functions are pure document operations over that envelope so the
router can commit the result through the governed write path; ``VersionedDoc``'s
own writers commit straight to the repo, which would bypass the protected-var
policy, review routing and audit every other mutation goes through.

An artefact is a gitcrud BUNDLE: a manifest describing the series, and the content
itself as real files beside it, one per version plus a copy of the marked one::

    config/library/<name>/manifest.yaml
    config/library/<name>/versions/0001.vrl
    config/library/<name>/versions/0002.vrl
    config/library/<name>/current.vrl

So a reviewer diffs VRL rather than a YAML block scalar, a validator runs against
a real path with a real extension, reading one version does not parse the whole
history, and no YAML emitter ever touches the bytes. ``current`` is a copy rather
than a symlink because it is read by Helm, by a bind mount and by git-sync alike,
and a dangling link fails differently in each.

Content is addressed by a sha256 digest over its canonical bytes: the
newline-terminated UTF-8 text for a text kind, the decoded bytes for a base64 one.
That makes republishing identical content a no-op, makes a divergence between a
linked version and the content resolved from it detectable, and lets ``current``
be checked against the manifest it is supposed to mirror.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dfe_engine.gitcrud.versioned import SPEC_KEY, VERSIONS_KEY, VersionConflictError

from .catalogue import ARTIFACT_KINDS, ArtifactKind, Encoding

_KIND_FIELD = "kind"
_STATE_FIELD = "state"
_CURRENT_FIELD = "current"
_GROUP_FIELD = "group"
_LABELS_FIELD = "labels"
_DESCRIPTION_FIELD = "description"
_TAGS_FIELD = "tags"

_FILE = "file"
_DIGEST = "digest"
_ENCODING = "encoding"
_SIZE = "size"
_TIMESTAMP = "at"

VERSIONS_DIR = "versions"
CURRENT_STEM = "current"

# A version is a file in a git repo, not an object in a blob store: the cap keeps
# a clone, a checkout and an Argo sync proportionate to configuration.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

# A label key or value, a group and a tag all end up in a YAML document and in a
# query string, so they keep to the same boring character set.
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")

# Control characters other than tab and newline produce a document the emitter
# writes and the parser then refuses.
_FORBIDDEN_TEXT = frozenset(chr(c) for c in range(0x20)) - {"\t", "\n", "\r"}


class LifecycleState(StrEnum):
    """An artefact's availability. Preferred over deletion so history stays intact."""

    ENABLED = "enabled"
    """Linkable, and re-resolving advances it."""

    DISABLED = "disabled"
    """Not linkable; existing links stay where they are."""

    DEPRECATED = "deprecated"
    """Still linkable, and reported as on the way out."""


class UnknownKindError(KeyError):
    """Raised when a kind is not declared in the manifest."""


class VersionNotFoundError(KeyError):
    """Raised when an artefact has no such version."""


class TagNotFoundError(KeyError):
    """Raised when an artefact carries no such tag."""


class InvalidArtifactError(ValueError):
    """Raised when content or metadata cannot be stored as an artefact."""


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    """One immutable version of an artefact, as the manifest describes it.

    The content lives at ``path`` inside the bundle, not in this record: a caller
    that wants the bytes reads that one file instead of the whole series.
    """

    version: int
    path: str
    digest: str
    encoding: Encoding
    kind: str
    size_bytes: int = 0
    description: str = ""
    published_by: str = ""
    published_at: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class BundlePlan:
    """The payload files a manifest change implies, for one atomic commit.

    Keeping the file moves beside the manifest edit is what stops a manifest
    naming a version whose content was never written.
    """

    writes: dict[str, str | bytes] = field(default_factory=dict)
    removals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactSummary:
    """An artefact's mutable metadata plus where its pointers sit."""

    name: str
    kind: str
    state: LifecycleState
    current: int | None
    versions: tuple[int, ...]
    tags: dict[str, int] = field(default_factory=dict)
    group: str = ""
    description: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    digest: str = ""


def kind(name: str) -> ArtifactKind:
    """Resolve a kind name against the manifest."""
    try:
        return ARTIFACT_KINDS[name]
    except KeyError:
        raise UnknownKindError(name) from None


def kinds() -> list[ArtifactKind]:
    """Every declared kind, in name order."""
    return [ARTIFACT_KINDS[name] for name in sorted(ARTIFACT_KINDS)]


def canonical_text(content: str) -> str:
    """Text content as it is stored: newline-terminated unless empty."""
    if not content or content.endswith("\n"):
        return content
    return content + "\n"


def canonical_bytes(artifact_kind: ArtifactKind, content: str) -> bytes:
    """The bytes a digest is taken over, and the bytes a consumer ends up with.

    Text is newline-terminated first, because the overlay stores it as a block
    scalar and that form always ends in a newline - digesting the unterminated
    form would report drift against content that had not changed.
    """
    if artifact_kind.encoding is Encoding.BASE64:
        # Whitespace is not part of the payload: a blob pasted out of a file
        # arrives line-wrapped, and the digest must not depend on that.
        try:
            return base64.b64decode("".join(content.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidArtifactError(
                f"{artifact_kind.name} content must be valid base64: {exc}"
            ) from exc
    return canonical_text(content).encode("utf-8")


def extension(artifact_kind: ArtifactKind) -> str:
    """The extension a file of this kind is stored under."""
    return artifact_kind.suffixes[0] if artifact_kind.suffixes else ""


def version_path(artifact_kind: ArtifactKind, version: int) -> str:
    """Where a version's content lives inside the bundle.

    Zero-padded so a directory listing sorts in version order.
    """
    return f"{VERSIONS_DIR}/{int(version):04d}{extension(artifact_kind)}"


def current_path(artifact_kind: ArtifactKind) -> str:
    """Where the marked version is mirrored, for anything reading the repo directly."""
    return f"{CURRENT_STEM}{extension(artifact_kind)}"


def as_text(artifact_kind: ArtifactKind, raw: bytes) -> str:
    """Stored bytes in the text form an overlay carries them in.

    A values file is YAML, so a binary kind travels to the chart base64-encoded
    even though it is stored on disk as itself.
    """
    if artifact_kind.encoding is Encoding.BASE64:
        return base64.b64encode(raw).decode("ascii")
    return raw.decode("utf-8")


def stored_content(artifact_kind: ArtifactKind, content: str) -> str | bytes:
    """Content in the exact form it is written to disk.

    A text kind is stored as its newline-terminated text; a base64 kind is decoded
    first, so a wasm module on disk is wasm rather than an encoding of it.
    """
    if artifact_kind.encoding is Encoding.BASE64:
        return canonical_bytes(artifact_kind, content)
    return canonical_text(content)


def digest_of(artifact_kind: ArtifactKind, content: str) -> str:
    """The content address of this content under this kind."""
    return f"sha256:{hashlib.sha256(canonical_bytes(artifact_kind, content)).hexdigest()}"


def validate_content(artifact_kind: ArtifactKind, content: str) -> None:
    """Reject content that cannot be stored, or that is too big to carry in git."""
    raw = canonical_bytes(artifact_kind, content)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise InvalidArtifactError(
            f"artefact is {len(raw)} bytes, over the {MAX_ARTIFACT_BYTES}-byte limit"
        )
    if artifact_kind.encoding is Encoding.TEXT:
        found = sorted(_FORBIDDEN_TEXT.intersection(content))
        if found:
            codes = ", ".join(f"\\x{ord(c):02x}" for c in found)
            raise InvalidArtifactError(
                f"content contains control characters YAML cannot carry ({codes})"
            )


def validate_token(what: str, value: str) -> str:
    """Reject a group, tag, label key or label value outside the safe character set."""
    if not _TOKEN_RE.fullmatch(value):
        raise InvalidArtifactError(
            f"invalid {what} {value!r}: expected alphanumerics, '.', '_', '/' and '-'"
        )
    return value


def validate_labels(labels: dict[str, str] | None) -> dict[str, str]:
    """Reject labels that are not a flat mapping of plain strings."""
    out: dict[str, str] = {}
    for key, value in (labels or {}).items():
        if not isinstance(value, str):
            raise InvalidArtifactError(f"label {key!r} must be a string")
        validate_token("label key", str(key))
        validate_token("label value", value)
        out[str(key)] = value
    return out


def new_artifact(
    artifact_kind: ArtifactKind,
    *,
    state: LifecycleState = LifecycleState.ENABLED,
    group: str = "",
    description: str = "",
    labels: dict[str, str] | None = None,
) -> dict:
    """The envelope for an artefact with no versions yet.

    Creating an artefact before it has content is what lets its kind, group and
    labels be settled before anything can be published into it.
    """
    return {
        _KIND_FIELD: artifact_kind.name,
        _STATE_FIELD: str(state),
        _GROUP_FIELD: validate_token("group", group) if group else "",
        _DESCRIPTION_FIELD: description,
        _LABELS_FIELD: validate_labels(labels),
        _TAGS_FIELD: {},
        _CURRENT_FIELD: None,
        "deployed": None,
        "status": "published",
        "draft": None,
        VERSIONS_KEY: {},
    }


def _versions(env: dict) -> dict[int, Any]:
    """The version map with integer keys, as YAML may load numeric keys as strings."""
    return {int(k): v for k, v in (env.get(VERSIONS_KEY) or {}).items()}


def artifact_kind_of(env: dict) -> ArtifactKind:
    """The kind an artefact was created as."""
    return kind(str(env.get(_KIND_FIELD, "")))


def state_of(env: dict) -> LifecycleState:
    """The artefact's lifecycle state."""
    try:
        return LifecycleState(str(env.get(_STATE_FIELD, LifecycleState.ENABLED)))
    except ValueError as exc:
        raise InvalidArtifactError(f"unknown lifecycle state {env.get(_STATE_FIELD)!r}") from exc


def set_state(env: dict, state: LifecycleState) -> bool:
    """Set the lifecycle state. Mutates ``env``. Returns whether it changed."""
    if state_of(env) is state:
        return False
    env[_STATE_FIELD] = str(state)
    return True


def set_metadata(
    env: dict,
    *,
    description: str | None = None,
    labels: dict[str, str] | None = None,
    group: str | None = None,
) -> bool:
    """Edit the classification metadata. Mutates ``env``. Returns whether it changed.

    Metadata is not content, so this never touches the version series. An omitted
    field is left alone; an empty group or an empty label map clears one.
    """
    changed = False
    if description is not None and description != str(env.get(_DESCRIPTION_FIELD, "")):
        env[_DESCRIPTION_FIELD] = description
        changed = True
    if group is not None:
        cleaned = validate_token("group", group) if group else ""
        if cleaned != str(env.get(_GROUP_FIELD, "")):
            env[_GROUP_FIELD] = cleaned
            changed = True
    if labels is not None:
        cleaned_labels = validate_labels(labels)
        if cleaned_labels != dict(env.get(_LABELS_FIELD) or {}):
            env[_LABELS_FIELD] = cleaned_labels
            changed = True
    return changed


def tags_of(env: dict) -> dict[str, int]:
    """Every tag on the artefact, mapped to the version it names."""
    return {str(k): int(v) for k, v in (env.get(_TAGS_FIELD) or {}).items()}


def set_tag(env: dict, tag: str, version: int) -> bool:
    """Point a tag at a version. Mutates ``env``. Returns whether it changed."""
    validate_token("tag", tag)
    if int(version) not in _versions(env):
        raise VersionNotFoundError(version)
    tags = tags_of(env)
    if tags.get(tag) == int(version):
        return False
    tags[tag] = int(version)
    env[_TAGS_FIELD] = tags
    return True


def delete_tag(env: dict, tag: str) -> bool:
    """Remove a tag. Mutates ``env``. Returns whether it changed."""
    tags = tags_of(env)
    if tag not in tags:
        raise TagNotFoundError(tag)
    del tags[tag]
    env[_TAGS_FIELD] = tags
    return True


def resolve_tag(env: dict, tag: str) -> int:
    """The version a tag names."""
    try:
        return tags_of(env)[tag]
    except KeyError:
        raise TagNotFoundError(tag) from None


def read_version(env: dict, version: int) -> ArtifactVersion:
    """One immutable version."""
    entry = _versions(env).get(int(version))
    if entry is None:
        raise VersionNotFoundError(version)
    spec = entry.get(SPEC_KEY) or {}
    return ArtifactVersion(
        version=int(version),
        path=str(spec.get(_FILE, "")),
        digest=str(spec.get(_DIGEST, "")),
        encoding=Encoding(str(spec.get(_ENCODING, Encoding.TEXT))),
        kind=str(spec.get(_KIND_FIELD, env.get(_KIND_FIELD, ""))),
        size_bytes=int(spec.get(_SIZE, 0)),
        description=str(spec.get(_DESCRIPTION_FIELD, "")),
        published_by=str(entry.get("by", "")),
        published_at=int(entry.get(_TIMESTAMP, 0)),
        message=str(entry.get("message", "")),
    )


def current_version(env: dict) -> ArtifactVersion | None:
    """The version the artefact's pointer currently names, if it has one."""
    cur = env.get(_CURRENT_FIELD)
    return None if cur is None else read_version(env, int(cur))


def version_numbers(env: dict) -> list[int]:
    """Ascending published version numbers."""
    return sorted(_versions(env))


def versions(env: dict) -> list[ArtifactVersion]:
    """Every version, oldest first."""
    return [read_version(env, v) for v in version_numbers(env)]


def summarise(name: str, env: dict) -> ArtifactSummary:
    """An artefact's metadata and pointers, without any content."""
    cur = current_version(env)
    return ArtifactSummary(
        name=name,
        kind=str(env.get(_KIND_FIELD, "")),
        state=state_of(env),
        current=None if cur is None else cur.version,
        versions=tuple(version_numbers(env)),
        tags=tags_of(env),
        group=str(env.get(_GROUP_FIELD, "")),
        description=str(env.get(_DESCRIPTION_FIELD, "")),
        labels=dict(env.get(_LABELS_FIELD) or {}),
        digest="" if cur is None else cur.digest,
    )


def publish(
    env: dict,
    *,
    content: str,
    actor: str,
    description: str = "",
    message: str = "",
    version: int | None = None,
    now: int | None = None,
) -> tuple[int, bool, BundlePlan]:
    """Freeze content as a version. Mutates ``env``. Returns (version, changed, plan).

    A version is immutable, so republishing the same content at a version that
    already holds it is a no-op reporting which version carries it, and publishing
    different content there raises. Without an explicit version the next number in
    the series is used, and re-sending the content the pointer already names is
    likewise a no-op.

    The plan carries the new version's file and the refreshed ``current`` copy;
    both must land in the same commit as the manifest.
    """
    artifact_kind = artifact_kind_of(env)
    validate_content(artifact_kind, content)
    digest = digest_of(artifact_kind, content)
    existing = _versions(env)
    next_version = (max(existing) + 1) if existing else 1

    if version is None:
        current = env.get(_CURRENT_FIELD)
        if current is not None and read_version(env, int(current)).digest == digest:
            return int(current), False, BundlePlan()
        target = next_version
    else:
        target = int(version)
        if target in existing:
            if read_version(env, target).digest == digest:
                return target, False, BundlePlan()
            raise VersionConflictError(
                f"version {target} already holds different content; publish a new version"
            )
        if target != next_version:
            raise VersionConflictError(
                f"version {target} is out of sequence; the next version is {next_version}"
            )

    stored = stored_content(artifact_kind, content)
    path = version_path(artifact_kind, target)
    existing[target] = {
        "by": actor,
        _TIMESTAMP: int(time.time()) if now is None else int(now),
        "message": message,
        SPEC_KEY: {
            _FILE: path,
            _DIGEST: digest,
            _ENCODING: str(artifact_kind.encoding),
            _KIND_FIELD: artifact_kind.name,
            _SIZE: len(canonical_bytes(artifact_kind, content)),
            _DESCRIPTION_FIELD: description,
        },
    }
    env[VERSIONS_KEY] = existing
    env[_CURRENT_FIELD] = target
    env["status"] = "published"
    env["draft"] = None
    plan = BundlePlan(writes={path: stored, current_path(artifact_kind): stored})
    return target, True, plan


def rollback(env: dict, version: int, content: str | bytes) -> tuple[bool, BundlePlan]:
    """Point ``current`` back at an earlier version. Mutates ``env``.

    Newer versions are kept: the pointer is mutable, the content is not. ``content``
    is that version's stored bytes, which the caller reads from the bundle, so the
    ``current`` copy is refreshed to match the pointer in the same commit.
    """
    if int(version) not in _versions(env):
        raise VersionNotFoundError(version)
    if env.get(_CURRENT_FIELD) == int(version):
        return False, BundlePlan()
    env[_CURRENT_FIELD] = int(version)
    env["status"] = "published"
    plan = BundlePlan(writes={current_path(artifact_kind_of(env)): content})
    return True, plan


def matches(
    summary: ArtifactSummary,
    *,
    kind_name: str = "",
    group: str = "",
    state: str = "",
    labels: list[str] | None = None,
    text: str = "",
) -> bool:
    """Whether an artefact passes the list filters, which AND together.

    Each label filter is either a bare key or ``key=value``, so a caller can ask
    for everything carrying a label as well as for one exact value. ``text`` is a
    case-insensitive substring of the name or the description.
    """
    if kind_name and summary.kind != kind_name:
        return False
    if group and summary.group != group:
        return False
    if state and str(summary.state) != state:
        return False
    for selector in labels or []:
        key, sep, value = selector.partition("=")
        if key not in summary.labels:
            return False
        if sep and summary.labels[key] != value:
            return False
    if text:
        needle = text.casefold()
        if needle not in summary.name.casefold() and needle not in summary.description.casefold():
            return False
    return True
