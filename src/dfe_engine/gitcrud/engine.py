#  Project:      dfe-engine
#  File:         gitcrud/engine.py
#  Purpose:      Generic YAML-in-git CRUD engine (Governed Ops foundation)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One engine for every resource class.

Enumerate resources, read + flatten to dot-paths, set/delete a single path, write
a whole doc, delete a resource - every mutation ends in ONE git commit via
GitopsRepo (dulwich, no git CLI). This is the single common path the survey called
for and the foundation Tier-1/Tier-2/operations build on.

A class declares its layout (see ``models.Layout``). FILE is one YAML document per
resource and the default. BUNDLE is a directory per resource - a manifest plus
payload files - for a class storing authored content that some tool other than the
engine parses; ``put_bundle`` writes the manifest and its files in one commit, and
every doc-level operation keeps working against the manifest unchanged.
"""

from __future__ import annotations

import builtins
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from dfe_engine.gitops.repo import GitopsRepo, PublishResult
from dfe_engine.yaml_utils import yaml_dump_string, yaml_load, yaml_load_string

from .commit_policy import validate_name, validate_resource_path
from .registry import ResourceClass, ResourceClassRegistry, default_registry

_MISSING = object()


class ResourceNotFoundError(FileNotFoundError):
    """Raised when a named resource does not exist in its class."""


class UnsafePathError(ValueError):
    """Raised when a bundle payload path would escape the resource's directory."""


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


def _set_path(doc: dict, dotpath: str, value: Any) -> None:
    parts = dotpath.split(".")
    cur = doc
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def set_path(doc: dict, dotpath: str, value: Any) -> None:
    """Public dot-path setter (creates intermediate dicts). Mutates ``doc``."""
    _set_path(doc, dotpath, value)


def get_path(doc: dict, dotpath: str, default: Any = None) -> Any:
    """Read a dot-path value, or ``default`` if absent."""
    cur: Any = doc
    for p in dotpath.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def del_path(doc: dict, dotpath: str) -> bool:
    """Public dot-path remover; returns whether anything was there. Mutates ``doc``."""
    return _del_path(doc, dotpath)


def _del_path(doc: dict, dotpath: str) -> bool:
    parts = dotpath.split(".")
    cur = doc
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            return False
        cur = nxt
    return cur.pop(parts[-1], _MISSING) is not _MISSING


class GitCrud:
    """Generic git-native CRUD over YAML resource classes.

    All git mechanics live in ``GitopsRepo``; this layer adds class resolution,
    YAML (de)serialisation, and dot-path edits. ``actor`` is recorded in the commit
    message; per-actor author/committer split + commit-policy come in the commit
    standard (Task 1.14).
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
    def repo(self):
        """The underlying GitopsRepo - break-glass raw publish/revert path."""
        return self._repo

    @property
    def registry(self) -> ResourceClassRegistry:
        """Public: the resource-class registry (read-only introspection)."""
        return self._registry

    @staticmethod
    def _check_name(cls: ResourceClass, name: str) -> None:
        """The one name rule, widened to path segments for a nested class."""
        if cls.nested:
            validate_resource_path(name)
        else:
            validate_name(name)

    def _rel(self, cls: ResourceClass, name: str) -> str:
        # Single chokepoint for every read/write path: the name must be safe to
        # splice into a file path AND a commit subject (see validate_name). For a
        # bundle this is the manifest, so every existing doc-level operation keeps
        # working against a directory-per-resource class unchanged.
        self._check_name(cls, name)
        if cls.is_bundle:
            return f"{cls.directory}/{name}/{cls.manifest}"
        return f"{cls.directory}/{name}{cls.suffix}"

    def _rel_dir(self, cls: ResourceClass, name: str) -> str:
        """A bundle resource's directory, repo-relative."""
        self._check_name(cls, name)
        return f"{cls.directory}/{name}"

    def _rel_payload(self, cls: ResourceClass, name: str, relpath: str) -> str:
        """A payload file inside a bundle, repo-relative and proven to stay inside.

        A payload path is caller-supplied, so it is resolved against the bundle
        directory and rejected unless it lands underneath it.
        """
        base = (self._repo.path / self._rel_dir(cls, name)).resolve()
        if not relpath or relpath != relpath.strip() or "\\" in relpath:
            raise UnsafePathError(f"invalid bundle path {relpath!r}")
        target = (base / relpath).resolve()
        if target == base or base not in target.parents:
            raise UnsafePathError(f"bundle path {relpath!r} escapes {self._rel_dir(cls, name)}")
        if target.name == cls.manifest and target.parent == base:
            raise UnsafePathError(f"{cls.manifest} is the manifest, not a payload file")
        return f"{self._rel_dir(cls, name)}/{Path(relpath).as_posix()}"

    def _file(self, cls: ResourceClass, name: str) -> Path:
        return self._repo.path / self._rel(cls, name)

    def reading(self) -> AbstractContextManager[None]:
        """Take any write another replica pushed, and hold the tree for the read.

        Each engine replica holds its own clone, so without the refresh a read
        serves whatever this pod last wrote and a write on one replica is invisible
        on every other one. One ref advertisement when the remote has not moved, and
        a burst of reads shares one. The block that follows holds the working tree
        against a concurrent publish or reset -- see GitopsRepo.read_locked.
        """
        return self._repo.read_locked()

    def list(self, cls_name: str) -> builtins.list[str]:
        """Enumerate resource names in a class.

        FILE layout: filenames minus the suffix. BUNDLE layout: subdirectories
        holding a manifest, so a half-written directory is not reported as a
        resource.
        """
        cls = self._cls(cls_name)
        with self.reading():
            directory = self._repo.path / cls.directory
            if not directory.is_dir():
                return []
            if cls.is_bundle:
                return sorted(p.name for p in directory.iterdir() if (p / cls.manifest).is_file())
            n = len(cls.suffix)
            # A nested class keeps its resources in a tree, so the name a caller
            # reads back is the path from the class directory, not the filename.
            pattern = f"**/*{cls.suffix}" if cls.nested else f"*{cls.suffix}"
            return sorted(
                p.relative_to(directory).as_posix()[:-n]
                for p in directory.glob(pattern)
                if p.is_file()
            )

    def get(self, cls_name: str, name: str) -> dict:
        """Read a resource's YAML doc."""
        cls = self._cls(cls_name)
        with self.reading():
            return self._read_doc(cls, name)

    def _read_doc(self, cls: ResourceClass, name: str) -> dict:
        """Load a resource's YAML doc off the working tree; caller holds the guard."""
        f = self._file(cls, name)
        if not f.is_file():
            raise ResourceNotFoundError(self._rel(cls, name))
        return yaml_load(f) or {}

    def get_remote(self, cls_name: str, name: str) -> dict:
        """Read a resource's YAML doc as the REMOTE deploy repo holds it right now.

        A write another replica pushed reaches this clone only on its next fetch, so a
        caller that must not act on a stale absence reads through here instead of
        :meth:`get`. Empty when there is no remote, the resource is absent there, or
        the file holds something other than a mapping.
        """
        cls = self._cls(cls_name)
        raw = self._repo.read_remote_file(self._rel(cls, name))
        if raw is None:
            return {}
        doc = yaml_load_string(raw)
        return doc if isinstance(doc, dict) else {}

    def vars(self, cls_name: str, name: str) -> dict[str, Any]:
        """Flatten a resource to dot-path vars (enumeration for Tier-1)."""
        return flatten(self.get(cls_name, name))

    def head_revision(self) -> str | None:
        """Current repo HEAD SHA - the optimistic-concurrency version token."""
        with self.reading():
            return self._repo.head_revision()

    def get_with_revision(self, cls_name: str, name: str) -> tuple[dict, str | None]:
        """Read a resource doc plus the current HEAD revision (for If-Match writes)."""
        cls = self._cls(cls_name)
        # One guard over both: a publish landing between them would hand back a doc
        # tagged with a revision it did not come from, and the If-Match write that
        # quotes it would overwrite that publish.
        with self.reading():
            return self._read_doc(cls, name), self._repo.head_revision()

    def _guard_revision(self, cls_name: str, name: str, base_revision: str | None) -> None:
        """Raise ConcurrencyConflictError if base_revision is stale (HEAD moved)."""
        if base_revision is None:
            return
        cls = self._cls(cls_name)
        with self.reading():
            head = self._repo.head_revision()
            if head is None or head == base_revision:
                return
            current = self._read_doc(cls, name) if self._file(cls, name).is_file() else {}
        raise ConcurrencyConflictError(cls_name, name, current, head)

    def put(
        self,
        cls_name: str,
        name: str,
        doc: dict,
        actor: str,
        message: str | None = None,
        base_revision: str | None = None,
        branch: str = "",
    ) -> PublishResult:
        """Write a whole doc and commit. If base_revision is given, enforce it.

        ``branch`` (non-empty) routes the commit to a review branch instead of the
        tracked branch -- the PR path the routing layer uses for production+team.
        """
        self._guard_revision(cls_name, name, base_revision)
        cls = self._cls(cls_name)
        msg = message or f"{cls.name}({name}): update by {actor}"
        return self._repo.publish(
            {self._rel(cls, name): yaml_dump_string(doc)}, msg, branch=branch or None
        )

    def put_many(
        self,
        items: builtins.list[tuple[str, str, dict]],
        actor: str,
        message: str,
        branch: str = "",
    ) -> PublishResult:
        """Write several resources in ONE commit (atomic actions).

        items: list of (cls_name, name, doc). All land in a single commit so a
        defined action that touches N resources is one atomic change. ``branch``
        routes them to a review branch (PR mode) instead of the tracked branch.
        """
        artifacts: dict[str, str] = {}
        for cls_name, name, doc in items:
            cls = self._cls(cls_name)
            artifacts[self._rel(cls, name)] = yaml_dump_string(doc)
        return self._repo.publish(artifacts, message, branch=branch or None)

    def payloads(self, cls_name: str, name: str) -> builtins.list[str]:
        """Every payload file in a bundle, as bundle-relative posix paths."""
        cls = self._require_bundle(cls_name)
        with self.reading():
            return self._payload_names(cls, name)

    def _payload_names(self, cls: ResourceClass, name: str) -> builtins.list[str]:
        """Walk a bundle's payload files; caller holds the guard."""
        base = self._repo.path / self._rel_dir(cls, name)
        if not base.is_dir():
            raise ResourceNotFoundError(self._rel_dir(cls, name))
        return sorted(
            p.relative_to(base).as_posix()
            for p in base.rglob("*")
            if p.is_file() and p.relative_to(base).as_posix() != cls.manifest
        )

    def read_payload_bytes(self, cls_name: str, name: str, relpath: str) -> bytes:
        """Read one payload file out of a bundle, verbatim."""
        cls = self._require_bundle(cls_name)
        rel = self._rel_payload(cls, name, relpath)
        with self.reading():
            target = self._repo.path / rel
            if not target.is_file():
                raise ResourceNotFoundError(rel)
            return target.read_bytes()

    def read_payload(self, cls_name: str, name: str, relpath: str) -> str:
        """Read one text payload file out of a bundle."""
        return self.read_payload_bytes(cls_name, name, relpath).decode("utf-8")

    def put_bundle(
        self,
        cls_name: str,
        name: str,
        doc: dict,
        actor: str,
        writes: dict[str, str | bytes] | None = None,
        removals: builtins.list[str] | None = None,
        message: str | None = None,
        base_revision: str | None = None,
        branch: str = "",
    ) -> PublishResult:
        """Write a bundle's manifest and payload files in ONE commit.

        The manifest and the files it describes move together, so a reader never
        sees a manifest naming content that is not there yet. ``writes`` and
        ``removals`` are bundle-relative paths.
        """
        cls = self._require_bundle(cls_name)
        self._guard_revision(cls_name, name, base_revision)
        artifacts: dict[str, str | bytes] = {self._rel(cls, name): yaml_dump_string(doc)}
        for relpath, content in (writes or {}).items():
            artifacts[self._rel_payload(cls, name, relpath)] = content
        deletions = [self._rel_payload(cls, name, r) for r in (removals or [])]
        msg = message or f"{cls.name}({name}): update by {actor}"
        return self._repo.publish(artifacts, msg, deletions=deletions, branch=branch or None)

    def _require_bundle(self, cls_name: str) -> ResourceClass:
        cls = self._cls(cls_name)
        if not cls.is_bundle:
            raise ValueError(f"resource class {cls_name!r} is not a bundle class")
        return cls

    def set_key(
        self,
        cls_name: str,
        name: str,
        dotpath: str,
        value: Any,
        actor: str,
        message: str | None = None,
        base_revision: str | None = None,
        branch: str = "",
    ) -> PublishResult:
        """Set a single dot-path (creating intermediates) and commit."""
        self._guard_revision(cls_name, name, base_revision)
        cls = self._cls(cls_name)
        # The doc is read before it is written, so a stale tree here writes back a
        # resource another replica's doc has moved on from.
        with self.reading():
            doc = self._read_doc(cls, name) if self._file(cls, name).is_file() else {}
        _set_path(doc, dotpath, value)
        msg = message or f"{cls.name}({name}): set {dotpath} by {actor}"
        return self.put(cls_name, name, doc, actor, msg, branch=branch)

    def delete_key(
        self,
        cls_name: str,
        name: str,
        dotpath: str,
        actor: str,
        message: str | None = None,
        branch: str = "",
    ) -> PublishResult:
        """Remove a single dot-path (revert to default) and commit."""
        cls = self._cls(cls_name)
        doc = self.get(cls_name, name)
        _del_path(doc, dotpath)
        msg = message or f"{cls.name}({name}): delete {dotpath} by {actor}"
        return self.put(cls_name, name, doc, actor, msg, branch=branch)

    def delete(
        self,
        cls_name: str,
        name: str,
        actor: str,
        message: str | None = None,
        base_revision: str | None = None,
        branch: str = "",
    ) -> PublishResult:
        """Delete a whole resource and commit. If base_revision is given, enforce it.

        A bundle takes its payload files with it, in the same commit as its
        manifest - a directory left holding orphaned content would be enumerated
        as neither present nor absent.

        A delete takes the same base_revision guard as a write: removing a
        resource somebody has edited since you read it destroys their change as
        thoroughly as overwriting it would.
        """
        self._guard_revision(cls_name, name, base_revision)
        cls = self._cls(cls_name)
        # Absence is decided against the tree, so a stale one 404s a resource
        # another replica created and leaves it deployed.
        with self.reading():
            if not self._file(cls, name).is_file():
                raise ResourceNotFoundError(self._rel(cls, name))
            deletions = [self._rel(cls, name)]
            if cls.is_bundle:
                deletions += [
                    self._rel_payload(cls, name, p) for p in self._payload_names(cls, name)
                ]
        msg = message or f"{cls.name}({name}): delete by {actor}"
        return self._repo.publish({}, msg, deletions=deletions, branch=branch or None)
