#  Project:      dfe-engine
#  File:         gitcrud/versioned.py
#  Purpose:      One per-artifact versioning scheme over GitCrud (draft/publish/rollback)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""VersionedDoc - the single per-artifact versioning authority.

No single YAML standard exists for in-file version history with draft/published, so
this follows the two recognised conventions it maps to (and the DFE meta-schema it
generalises): monotonic INTEGER versions per artifact (Confluent Schema Registry
model) + a ``status: draft|published`` field (the change-log convention - there is
no built-in keyword) + per-version metadata like a change log. Envelope::

    current: 2            # latest PUBLISHED version (consumers read this)
    deployed: 1           # what Argo/the cluster actually has (commit != deployment)
    status: published     # draft | published
    draft: {...}          # the mutable working payload (None once published, no edits)
    versions:
      1: {by: kaz, message: "...", spec: {...}}   # metadata + the payload
      2: {by: kaz, message: "...", spec: {...}}

Each op is ONE git commit via GitCrud (audit); the version series lives IN the doc -
a per-artifact grouping, NOT repo git-log. Published snapshots are immutable (never
overwritten - the Sources append-only rule). See
docs/superpowers/plans/2026-07-01-rules-hunts-versioning.md.
"""

from __future__ import annotations

import copy
from typing import Any

from .engine import GitCrud, ResourceNotFoundError

_VERSIONS = "versions"
_SPEC = "spec"


class VersionConflictError(Exception):
    """Raised on an attempt to overwrite an existing published version."""


class VersionedDoc:
    """Draft -> publish -> rollback lifecycle for versioned GitCrud classes."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = crud

    def _require_versioned(self, cls: str) -> None:
        """Enforce the opt-in: refuse a class that did not set ``versioned=True``.

        A versioned=False class (helmvars, actions, and other dial classes) is
        edited directly via GitCrud - its git log IS its history. Guarding at the
        two chokepoints (_envelope for every read/read-before-write, _save for
        every write) means such a class can never accidentally be wrapped in a
        draft/publish version envelope - the opt-in is a guarantee, not a
        convention. Unknown classes raise from the registry.
        """
        if not self._crud.resource_class(cls).versioned:
            raise ValueError(
                f"resource class {cls!r} is not versioned (versioned=False); "
                "edit it directly via GitCrud, or set versioned=True to opt in"
            )

    # ---- envelope load ------------------------------------------------

    def _envelope(self, cls: str, name: str) -> dict[str, Any] | None:
        """The version envelope, or None if the resource does not exist.

        VersionedDoc owns every write, so a stored doc is always an envelope; a doc
        with no ``versions`` map reads as "no published versions" (no legacy
        flat-doc back-compat - nothing is GA to migrate from).
        """
        self._require_versioned(cls)
        try:
            return self._crud.get(cls, name)
        except ResourceNotFoundError:
            return None

    @staticmethod
    def _versions(env: dict) -> dict[int, Any]:
        """Version map with int keys (YAML may load numeric keys as str)."""
        try:
            return {int(k): v for k, v in (env.get(_VERSIONS) or {}).items()}
        except (TypeError, ValueError) as e:
            # e.g. the `sources` class: semver keys, envelope owned by the
            # Source model itself - not the integer draft/publish lifecycle.
            raise ValueError(
                "resource carries a self-managed (non-integer) version envelope; "
                "its lifecycle is owned by its own model, not VersionedDoc"
            ) from e

    def _save(self, cls: str, name: str, env: dict, actor: str, msg: str):
        self._require_versioned(cls)
        return self._crud.put(cls, name, env, actor, message=msg)

    # ---- reads --------------------------------------------------------

    def get_published(self, cls: str, name: str) -> dict | None:
        """The current published version's payload (spec), or None."""
        env = self._envelope(cls, name)
        if env is None:
            return None
        cur = env.get("current")
        if cur is None:
            return None
        entry = self._versions(env).get(int(cur))
        return copy.deepcopy(entry.get(_SPEC)) if entry else None

    def get_draft(self, cls: str, name: str) -> dict | None:
        """The working draft payload, or None."""
        env = self._envelope(cls, name)
        return copy.deepcopy(env.get("draft")) if env else None

    def get_version(self, cls: str, name: str, version: int) -> dict | None:
        """A specific published version's payload (spec), or None."""
        env = self._envelope(cls, name)
        if env is None:
            return None
        entry = self._versions(env).get(int(version))
        return copy.deepcopy(entry.get(_SPEC)) if entry else None

    def list_versions(self, cls: str, name: str) -> list[int]:
        """Ascending list of published version numbers."""
        env = self._envelope(cls, name)
        return sorted(self._versions(env)) if env else []

    def versions_meta(self, cls: str, name: str) -> list[dict[str, Any]]:
        """Per-version metadata (version, by, message) for the UI - no payloads."""
        env = self._envelope(cls, name)
        if env is None:
            return []
        out = []
        for ver in sorted(self._versions(env)):
            entry = self._versions(env)[ver]
            out.append({"version": ver, "by": entry.get("by"), "message": entry.get("message", "")})
        return out

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

    def publish(self, cls: str, name: str, actor: str, message: str = "") -> int:
        """Freeze the draft as the next immutable version; return its number."""
        env = self._envelope(cls, name)
        if env is None or env.get("draft") is None:
            raise ValueError(f"{cls}/{name}: no draft to publish")
        versions = self._versions(env)
        new_ver = (max(versions) + 1) if versions else 1
        if new_ver in versions:  # never overwrite a published snapshot
            raise VersionConflictError(f"{cls}/{name}: version {new_ver} already exists")
        versions[new_ver] = {"by": actor, "message": message, _SPEC: copy.deepcopy(env["draft"])}
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
