#  Project:      dfe-engine
#  File:         gitcrud/models.py
#  Purpose:      Resource-class descriptors for the generic git-native CRUD engine
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Descriptors for Governed Ops resource classes.

A ResourceClass says WHERE a class of YAML resources lives (repo-relative
directory + file suffix) and the RBAC action prefix that governs it. The generic
engine handles every class uniformly, and authz is bound once at the class level -
never per file or per field (see docs/ARCHITECTURE.md, "Governed Ops").
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceClass:
    """One Governed Ops CRUD class.

    name        logical class name, e.g. "helmvars", "hunts", "governance".
    directory   repo-relative directory holding the class's YAML files.
    rbac_prefix RBAC action prefix; defaults to ``name``. Grants are bound here,
                e.g. ``helmvars:write`` covers every file in the class.
    suffix      file extension (".yaml").
    versioned   opt-in: the class's docs carry a per-artifact version envelope
                ({current, versions, draft, ...}) managed by VersionedDoc. A dial
                class (helmvars, actions) is unversioned - the git log is enough.
    """

    name: str
    directory: str
    rbac_prefix: str = ""
    suffix: str = ".yaml"
    versioned: bool = False

    def action(self, verb: str) -> str:
        """RBAC action string for a verb, e.g. ``helmvars:write``."""
        return f"{self.rbac_prefix or self.name}:{verb}"
