#  Project:      dfe-engine
#  File:         gitcrud/models.py
#  Purpose:      Resource-class descriptors for the generic git-native CRUD engine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Descriptors for Governed Ops resource classes.

A ResourceClass says WHERE a class of resources lives (repo-relative directory +
file suffix), HOW one is laid out on disk, and the RBAC action prefix that governs
it. The generic engine handles every class uniformly, and authz is bound once at
the class level - never per file or per field (see docs/architecture.md, "Governed
Ops").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Layout(StrEnum):
    """How one resource of a class is laid out in the repo."""

    FILE = "file"
    """One YAML document, one file. The default, and right for structured config:
    the fields ARE the resource, so a single doc keeps them consistent by
    construction and gives one conflict domain per write."""

    BUNDLE = "bundle"
    """A directory per resource: a YAML manifest plus payload files beside it.

    For resources carrying opaque authored CONTENT - bytes some tool other than
    the engine parses. Keeping content out of the manifest means a real diff of
    the real language, tooling that works on a real path with a real extension,
    a read cost per file rather than per history, and no YAML emitter anywhere
    near the bytes.
    """


@dataclass(frozen=True)
class ResourceClass:
    """One Governed Ops CRUD class.

    name        logical class name, e.g. "helmvars", "hunts", "governance".
    directory   repo-relative directory holding the class's resources.
    rbac_prefix RBAC action prefix; defaults to ``name``. Grants are bound here,
                e.g. ``helmvars:write`` covers every file in the class.
    suffix      file extension for a FILE-layout resource (".yaml").
    versioned   opt-in: the class's docs carry a per-artifact version envelope
                ({current, versions, draft, ...}) managed by VersionedDoc. A dial
                class (helmvars, actions) is unversioned - the git log is enough.
    layout      FILE (one doc, one file) or BUNDLE (a directory per resource).
    manifest    BUNDLE only: the manifest filename inside a resource's directory.
    nested      opt-in: a name may carry ``/`` segments, so the class's resources
                live in a tree (``beats/filebeat_auth``). Every segment still goes
                through the same name rule; only the separator is permitted.
    """

    name: str
    directory: str
    rbac_prefix: str = ""
    suffix: str = ".yaml"
    versioned: bool = False
    layout: Layout = Layout.FILE
    manifest: str = "manifest.yaml"
    nested: bool = False

    @property
    def is_bundle(self) -> bool:
        return self.layout is Layout.BUNDLE

    def action(self, verb: str) -> str:
        """RBAC action string for a verb, e.g. ``helmvars:write``."""
        return f"{self.rbac_prefix or self.name}:{verb}"
