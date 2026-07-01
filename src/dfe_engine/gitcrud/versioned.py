#  Project:      dfe-engine
#  File:         gitcrud/versioned.py
#  Purpose:      One per-artifact versioning scheme over GitCrud (draft/publish/rollback)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""VersionedDoc - the single per-artifact versioning authority.

A versioned artifact is ONE YAML doc with an envelope::

    current: 2            # latest PUBLISHED version (consumers read this)
    deployed: 1           # what Argo/the cluster actually has (commit != deployment)
    status: published     # draft | published
    draft: {...}          # the mutable working copy (None once published, no new edits)
    versions:
      1: {...}            # immutable published snapshots (arbitrary payload)
      2: {...}

Version numbers are monotonic integers. Each op is ONE git commit via GitCrud (audit
trail); the version series lives IN the doc - a per-artifact grouping, NOT repo
git-log. A pre-versioning FLAT doc reads as an implicit single published version (v1),
so adopters migrate without a hard cutover (mirrors schema_loader's flat fallback).
Published snapshots are immutable - never overwritten (mirrors the Sources
append-only rule). See docs/superpowers/plans/2026-07-01-rules-hunts-versioning.md.
"""

from __future__ import annotations

import copy
from typing import Any

from .engine import GitCrud, ResourceNotFoundError

_VERSIONS = "versions"


class VersionConflictError(Exception):
    """Raised on an attempt to overwrite an existing published version."""


class VersionedDoc:
    """Draft -> publish -> rollback lifecycle for versioned GitCrud classes."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = crud

    # ---- envelope load ------------------------------------------------

    def _envelope(self, cls: str, name: str) -> dict[str, Any] | None:
        """The version envelope, or None if the resource does not exist.

        A flat legacy doc (no ``versions`` map) is wrapped in-memory as an implicit
        single published version - it is only rewritten to envelope form on the next
        write.
        """
        try:
            raw = self._crud.get(cls, name)
        except ResourceNotFoundError:
            return None
        if isinstance(raw.get(_VERSIONS), dict):
            return raw
        return {
            "current": 1,
            "deployed": None,
            "status": "published",
            "draft": None,
            _VERSIONS: {1: raw},
        }

    @staticmethod
    def _versions(env: dict) -> dict[int, Any]:
        """Version map with int keys (YAML may load numeric keys as str)."""
        return {int(k): v for k, v in (env.get(_VERSIONS) or {}).items()}

    def _save(self, cls: str, name: str, env: dict, actor: str, msg: str):
        return self._crud.put(cls, name, env, actor, message=msg)

    # ---- reads --------------------------------------------------------

    def get_published(self, cls: str, name: str) -> dict | None:
        """The current published version's payload, or None if nothing published."""
        env = self._envelope(cls, name)
        if env is None:
            return None
        cur = env.get("current")
        if cur is None:
            return None
        return copy.deepcopy(self._versions(env).get(int(cur)))

    def get_draft(self, cls: str, name: str) -> dict | None:
        """The working draft payload, or None."""
        env = self._envelope(cls, name)
        return copy.deepcopy(env.get("draft")) if env else None

    def get_version(self, cls: str, name: str, version: int) -> dict | None:
        """A specific published version's payload, or None."""
        env = self._envelope(cls, name)
        return copy.deepcopy(self._versions(env).get(int(version))) if env else None

    def list_versions(self, cls: str, name: str) -> list[int]:
        """Ascending list of published version numbers."""
        env = self._envelope(cls, name)
        return sorted(self._versions(env)) if env else []

    def status(self, cls: str, name: str) -> str | None:
        env = self._envelope(cls, name)
        return env.get("status") if env else None

    def deployed(self, cls: str, name: str) -> int | None:
        """The version pointer for what is actually reconciled (Argo), if tracked."""
        env = self._envelope(cls, name)
        return env.get("deployed") if env else None

    # ---- writes (each = one git commit) -------------------------------

    def save_draft(self, cls: str, name: str, doc: dict, actor: str):
        """Set the working draft (creates the artifact if new)."""
        env = self._envelope(cls, name) or {
            "current": None,
            "deployed": None,
            "status": "published",
            "draft": None,
            _VERSIONS: {},
        }
        env["draft"] = doc
        env["status"] = "draft"
        return self._save(cls, name, env, actor, f"{cls}({name}): draft by {actor}")

    def publish(self, cls: str, name: str, actor: str) -> int:
        """Freeze the draft as the next immutable version; return its number."""
        env = self._envelope(cls, name)
        if env is None or env.get("draft") is None:
            raise ValueError(f"{cls}/{name}: no draft to publish")
        versions = self._versions(env)
        new_ver = (max(versions) + 1) if versions else 1
        if new_ver in versions:  # never overwrite a published snapshot
            raise VersionConflictError(f"{cls}/{name}: version {new_ver} already exists")
        versions[new_ver] = copy.deepcopy(env["draft"])
        env[_VERSIONS] = versions
        env["current"] = new_ver
        env["draft"] = None
        env["status"] = "published"
        self._save(cls, name, env, actor, f"{cls}({name}): publish v{new_ver} by {actor}")
        return new_ver

    def rollback(self, cls: str, name: str, version: int, actor: str) -> None:
        """Point ``current`` back at an earlier version (never deletes newer ones)."""
        env = self._envelope(cls, name)
        if env is None:
            raise ResourceNotFoundError(f"{cls}/{name}")
        if int(version) not in self._versions(env):
            raise ValueError(f"{cls}/{name}: no version {version}")
        env["current"] = int(version)
        env["status"] = "published"
        self._save(cls, name, env, actor, f"{cls}({name}): rollback to v{version} by {actor}")

    def set_deployed(self, cls: str, name: str, version: int, actor: str) -> None:
        """Record which version is actually reconciled onto the cluster."""
        env = self._envelope(cls, name)
        if env is None:
            raise ResourceNotFoundError(f"{cls}/{name}")
        env["deployed"] = int(version)
        self._save(cls, name, env, actor, f"{cls}({name}): mark deployed v{version} by {actor}")
