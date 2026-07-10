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
from pathlib import Path
from typing import Any

from dfe_engine.gitops.repo import GitopsRepo, PublishResult
from dfe_engine.yaml_utils import yaml_dump_string, yaml_load

from .commit_policy import validate_name
from .registry import ResourceClass, ResourceClassRegistry, default_registry

_MISSING = object()


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

    def _rel(self, cls: ResourceClass, name: str) -> str:
        # Single chokepoint for every read/write path: the name must be safe to
        # splice into a file path AND a commit subject (see validate_name).
        validate_name(name)
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
        doc = self.get(cls_name, name) if self._file(cls, name).is_file() else {}
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
        branch: str = "",
    ) -> PublishResult:
        """Delete a whole resource and commit."""
        cls = self._cls(cls_name)
        if not self._file(cls, name).is_file():
            raise ResourceNotFoundError(self._rel(cls, name))
        msg = message or f"{cls.name}({name}): delete by {actor}"
        return self._repo.publish({}, msg, deletions=[self._rel(cls, name)], branch=branch or None)
