#  Project:      dfe-engine
#  File:         gitcrud/engine.py
#  Purpose:      Generic YAML-in-git CRUD engine (Governed Ops foundation)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One engine for every YAML resource class.

Enumerate files, read + flatten to dot-paths, set/delete a single path, write a
whole doc, delete a resource - every mutation ends in ONE git commit via
GitopsRepo (dulwich, no git CLI). This is the single common path the survey called
for and the foundation Tier-1/Tier-2/operations build on.
"""

from __future__ import annotations

import builtins
import re
from pathlib import Path
from typing import Any

from dfe_engine.gitops.repo import GitopsRepo, PublishResult
from dfe_engine.yaml_utils import yaml_dump_string, yaml_load

from .commit_policy import CommitContext, build_message, type_for_class
from .registry import ResourceClass, ResourceClassRegistry, default_registry

_MISSING = object()


def _ensure_actor_trailer(message: str, actor: str) -> str:
    """Guarantee a ``DFE-Actor`` trailer so read_log attributes the commit.

    Without it read_log falls back to the git author ('dfe-engine') and marks the
    commit mis-attributed. A no-op when the trailer is already present (every
    build_message default path adds it). Appended after the body/``[skip ci]`` -
    read_log scans EVERY line for DFE-* trailers, so position is irrelevant; this
    is the safety net that also attributes explicit-message callers that skipped
    build_message (e.g. VersionedDoc).
    """
    for line in message.splitlines():
        if line.startswith("DFE-Actor:"):
            return message
    return f"{message}\nDFE-Actor: {actor}"


class ResourceNotFoundError(FileNotFoundError):
    """Raised when a named resource does not exist in its class."""


class ConcurrencyConflictError(Exception):
    """Raised when a write's base revision is stale (HEAD moved underneath it).

    Carries the current doc + revision so the caller (router) can build a 3-way
    view: current-at-head vs the caller's intended change vs theirs.
    """

    def __init__(self, cls: str, name: str, current: dict, head: str | None) -> None:
        super().__init__(f"{cls}/{name}: base revision is stale (HEAD={head})")
        self.cls = cls
        self.name = name
        self.current = current
        self.head = head


def flatten(doc: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict/list to dot-paths (list items indexed as ``key[i]``)."""
    out: dict[str, Any] = {}
    if isinstance(doc, dict):
        for k, v in doc.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(flatten(v, key))
            else:
                out[key] = v
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            key = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                out.update(flatten(v, key))
            else:
                out[key] = v
    elif prefix:
        out[prefix] = doc
    return out


# flatten() emits list elements as name[i] (chained for nested lists), so the
# walkers must parse that shape back or a round-tripped path silently lands as
# a literal 'name[0]' dict key. Anything NOT of this exact shape stays a plain
# dict key - dict behaviour is unchanged.
_SEGMENT_RE = re.compile(r"^(?P<name>[^\[]*)(?P<indexes>(?:\[\d+\])+)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def _parse_segment(seg: str) -> tuple[str, list[int]]:
    """Split ``name[0][1]`` into ``('name', [0, 1])``; plain keys get no indexes."""
    m = _SEGMENT_RE.match(seg)
    if not m:
        return seg, []
    return m.group("name"), [int(i) for i in _INDEX_RE.findall(m.group("indexes"))]


def _resolve_list_slot(cur: Any, name: str, indexes: list[int], seg: str) -> tuple[list, int]:
    """Resolve an indexed segment to ``(list, last_index)`` for writes.

    Writes must never invent list elements or fall back to a literal
    ``name[0]`` dict key, so any miss is a hard error naming the segment.
    """
    node = cur
    if name:
        if not isinstance(node, dict) or name not in node:
            raise ValueError(f"path segment {seg!r}: {name!r} is not an existing key")
        node = node[name]
    for idx in indexes[:-1]:
        if not isinstance(node, list) or idx >= len(node):
            raise ValueError(f"path segment {seg!r}: index [{idx}] is not an existing list element")
        node = node[idx]
    last = indexes[-1]
    if not isinstance(node, list) or last >= len(node):
        raise ValueError(f"path segment {seg!r}: index [{last}] is not an existing list element")
    return node, last


def _set_path(doc: dict, dotpath: str, value: Any) -> None:
    parts = dotpath.split(".")
    cur = doc
    for p in parts[:-1]:
        name, indexes = _parse_segment(p)
        if indexes:
            lst, idx = _resolve_list_slot(cur, name, indexes, p)
            nxt = lst[idx]
            if not isinstance(nxt, dict):
                nxt = {}
                lst[idx] = nxt
        else:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
        cur = nxt
    name, indexes = _parse_segment(parts[-1])
    if indexes:
        lst, idx = _resolve_list_slot(cur, name, indexes, parts[-1])
        lst[idx] = value
    else:
        cur[parts[-1]] = value


def set_path(doc: dict, dotpath: str, value: Any) -> None:
    """Public dot-path setter (creates intermediate dicts). Mutates ``doc``."""
    _set_path(doc, dotpath, value)


def get_path(doc: dict, dotpath: str, default: Any = None) -> Any:
    """Read a dot-path value, or ``default`` if absent."""
    cur: Any = doc
    for p in dotpath.split("."):
        name, indexes = _parse_segment(p)
        if name or not indexes:
            if not isinstance(cur, dict) or name not in cur:
                return default
            cur = cur[name]
        for idx in indexes:
            if not isinstance(cur, list) or idx >= len(cur):
                return default
            cur = cur[idx]
    return cur


def _del_path(doc: dict, dotpath: str) -> bool:
    parts = dotpath.split(".")
    cur: Any = doc
    for p in parts[:-1]:
        name, indexes = _parse_segment(p)
        if name or not indexes:
            if not isinstance(cur, dict) or name not in cur:
                return False
            cur = cur[name]
        for idx in indexes:
            if not isinstance(cur, list) or idx >= len(cur):
                return False
            cur = cur[idx]
    name, indexes = _parse_segment(parts[-1])
    if indexes:
        if name:
            if not isinstance(cur, dict) or name not in cur:
                return False
            cur = cur[name]
        for idx in indexes[:-1]:
            if not isinstance(cur, list) or idx >= len(cur):
                return False
            cur = cur[idx]
        last = indexes[-1]
        if not isinstance(cur, list) or last >= len(cur):
            return False
        del cur[last]
        return True
    if not isinstance(cur, dict):
        return False
    return cur.pop(name, _MISSING) is not _MISSING


class GitCrud:
    """Generic git-native CRUD over YAML resource classes.

    All git mechanics live in ``GitopsRepo``; this layer adds class resolution,
    YAML (de)serialisation, and dot-path edits. Every default commit message is
    rendered to the commit standard (``type(scope): summary`` + a ``DFE-Actor``
    trailer for ``actor``) so the gitcrud audit log attributes each change to the
    acting user, not the git author 'dfe-engine' (see commit_policy + log.py).
    """

    def __init__(
        self,
        repo: GitopsRepo,
        registry: ResourceClassRegistry | None = None,
    ) -> None:
        self._repo = repo
        self._registry = registry or default_registry()
        self._repo.ensure()

    def _cls(self, cls_name: str) -> ResourceClass:
        return self._registry.get(cls_name)

    def resource_class(self, cls_name: str) -> ResourceClass:
        """Public: resolve a class descriptor (name, directory, rbac_prefix)."""
        return self._cls(cls_name)

    @property
    def repo_path(self) -> Path:
        """Working-tree path of the deploy repo (read-only introspection)."""
        return self._repo.path

    @property
    def repo(self) -> GitopsRepo:
        """The underlying GitopsRepo.

        Lets a subsystem build a second GitCrud over the SAME working clone but with
        its OWN resource-class registry (e.g. the sigma catalogue's sigma_rules /
        sigma_selections classes) without registering those classes on the shared
        default registry. Both wrappers are stateless over the one repo.
        """
        return self._repo

    @property
    def registry(self) -> ResourceClassRegistry:
        """Public: the resource-class registry (read-only introspection)."""
        return self._registry

    def _default_message(self, cls: ResourceClass, name: str, summary: str, actor: str) -> str:
        """Conforming default commit message (``type(scope): summary`` + DFE-Actor).

        type_for_class keeps the subject an ALLOWED commit type (helmvars->cfg,
        governance->rbac, ...) so read_log marks it conforming, and the threaded
        actor becomes the DFE-Actor trailer so it is attributed. build_message
        budget-truncates an over-long subject rather than raising.
        """
        return build_message(
            CommitContext(
                ctype=type_for_class(cls.rbac_prefix or cls.name),
                scope=name,
                summary=summary,
                actor=actor,
            )
        )

    @staticmethod
    def _validate_name(name: str) -> str:
        """Refuse a resource name that could escape its class directory.

        ``_rel`` turns ``name`` into ``directory/name.suffix``, so a name carrying
        a path separator, a parent/current-dir ref or a NUL byte would let a write
        or delete land OUTSIDE the class directory (F-GITCRUD-TRAVERSAL). A resource
        name is a single filename stem - reject anything that is not. GitopsRepo
        re-checks containment as defence in depth, but failing here keeps the bad
        name out of the commit message and the publish entirely.
        """
        if not name or name in (".", ".."):
            raise ValueError(f"invalid resource name: {name!r}")
        if "/" in name or "\\" in name or "\x00" in name:
            raise ValueError(f"invalid resource name (path separator or NUL): {name!r}")
        return name

    def _rel(self, cls: ResourceClass, name: str) -> str:
        self._validate_name(name)
        return f"{cls.directory}/{name}{cls.suffix}"

    def _file(self, cls: ResourceClass, name: str) -> Path:
        return self._repo.path / self._rel(cls, name)

    def list(self, cls_name: str) -> builtins.list[str]:
        """Enumerate resource names in a class (filenames minus the suffix)."""
        cls = self._cls(cls_name)
        directory = self._repo.path / cls.directory
        if not directory.is_dir():
            return []
        n = len(cls.suffix)
        return sorted(p.name[:-n] for p in directory.glob(f"*{cls.suffix}") if p.is_file())

    def get(self, cls_name: str, name: str) -> dict:
        """Read a resource's YAML doc."""
        cls = self._cls(cls_name)
        f = self._file(cls, name)
        if not f.is_file():
            raise ResourceNotFoundError(self._rel(cls, name))
        return yaml_load(f) or {}

    def vars(self, cls_name: str, name: str) -> dict[str, Any]:
        """Flatten a resource to dot-path vars (enumeration for Tier-1)."""
        return flatten(self.get(cls_name, name))

    def head_revision(self) -> str | None:
        """Current repo HEAD SHA - the optimistic-concurrency version token."""
        return self._repo.head_revision()

    def get_with_revision(self, cls_name: str, name: str) -> tuple[dict, str | None]:
        """Read a resource doc plus the current HEAD revision (for If-Match writes)."""
        return self.get(cls_name, name), self.head_revision()

    def _guard_revision(self, cls_name: str, name: str, base_revision: str | None) -> None:
        """Raise ConcurrencyConflictError if base_revision is stale (HEAD moved)."""
        if base_revision is None:
            return
        head = self.head_revision()
        if head is not None and head != base_revision:
            cls = self._cls(cls_name)
            current = self.get(cls_name, name) if self._file(cls, name).is_file() else {}
            raise ConcurrencyConflictError(cls_name, name, current, head)

    def put(
        self,
        cls_name: str,
        name: str,
        doc: dict,
        actor: str,
        message: str | None = None,
        base_revision: str | None = None,
    ) -> PublishResult:
        """Write a whole doc and commit. If base_revision is given, enforce it."""
        self._guard_revision(cls_name, name, base_revision)
        cls = self._cls(cls_name)
        msg = message or self._default_message(cls, name, "update", actor)
        msg = _ensure_actor_trailer(msg, actor)
        return self._repo.publish({self._rel(cls, name): yaml_dump_string(doc)}, msg)

    def put_many(
        self,
        items: builtins.list[tuple[str, str, dict]],
        actor: str,
        message: str,
    ) -> PublishResult:
        """Write several resources in ONE commit (atomic actions).

        items: list of (cls_name, name, doc). All land in a single commit so a
        defined action that touches N resources is one atomic change.
        """
        artifacts: dict[str, str] = {}
        for cls_name, name, doc in items:
            cls = self._cls(cls_name)
            artifacts[self._rel(cls, name)] = yaml_dump_string(doc)
        return self._repo.publish(artifacts, _ensure_actor_trailer(message, actor))

    def set_key(
        self,
        cls_name: str,
        name: str,
        dotpath: str,
        value: Any,
        actor: str,
        message: str | None = None,
        base_revision: str | None = None,
    ) -> PublishResult:
        """Set a single dot-path (creating intermediates) and commit."""
        self._guard_revision(cls_name, name, base_revision)
        cls = self._cls(cls_name)
        doc = self.get(cls_name, name) if self._file(cls, name).is_file() else {}
        _set_path(doc, dotpath, value)
        msg = message or self._default_message(cls, name, f"set {dotpath}", actor)
        return self.put(cls_name, name, doc, actor, msg)

    def delete_key(
        self,
        cls_name: str,
        name: str,
        dotpath: str,
        actor: str,
        message: str | None = None,
        base_revision: str | None = None,
    ) -> PublishResult:
        """Remove a single dot-path (revert to default) and commit.

        A revert is still a WRITE, so it honours the same optimistic-concurrency
        If-Match check as set_key when ``base_revision`` is supplied.
        """
        self._guard_revision(cls_name, name, base_revision)
        cls = self._cls(cls_name)
        doc = self.get(cls_name, name)
        _del_path(doc, dotpath)
        msg = message or self._default_message(cls, name, f"delete {dotpath}", actor)
        return self.put(cls_name, name, doc, actor, msg)

    def delete(
        self,
        cls_name: str,
        name: str,
        actor: str,
        message: str | None = None,
    ) -> PublishResult:
        """Delete a whole resource and commit."""
        cls = self._cls(cls_name)
        if not self._file(cls, name).is_file():
            raise ResourceNotFoundError(self._rel(cls, name))
        msg = message or self._default_message(cls, name, "delete", actor)
        msg = _ensure_actor_trailer(msg, actor)
        return self._repo.publish({}, msg, deletions=[self._rel(cls, name)])
