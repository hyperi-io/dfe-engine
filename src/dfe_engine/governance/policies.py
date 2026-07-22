#  Project:      dfe-engine
#  File:         governance/policies.py
#  Purpose:      Protected-var policy: lock vars to default (Governed Ops guardrail)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Protected-var policies, stored as YAML in the gitops governance class.

A policy lists `cls:name:path` glob patterns that are LOCKED. The generic engine's
Tier-1 writes and Tier-2 actions consult ``is_protected`` before mutating; a
protected var changes only when the caller holds the override grant (the router
gates ``helmvars:override`` / ``governance:override``). This is the one thing that
reaches below a class - a policy object, not a per-var ACL.
"""

from __future__ import annotations

import builtins
from fnmatch import fnmatch

from dfe_engine.gitcrud import GitCrud

from .models import ProtectedPolicy

_POLICY_CLASS = "policies"


class ProtectedVarError(PermissionError):
    """Raised when a protected var is written without the override grant."""

    def __init__(self, cls: str, name: str, path: str, pattern: str) -> None:
        super().__init__(f"{cls}:{name}:{path} is protected by policy pattern '{pattern}'")
        self.cls = cls
        self.name = name
        self.path = path
        self.pattern = pattern


class PolicyStore:
    """Loads protected-var policies from the gitops governance class via GitCrud."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = crud

    def list(self) -> builtins.list[str]:
        return self._crud.list(_POLICY_CLASS)

    def get(self, name: str) -> ProtectedPolicy:
        return ProtectedPolicy.model_validate(self._crud.get(_POLICY_CLASS, name))

    def all_patterns(self) -> list[str]:
        """Aggregate every protected pattern across all policy docs."""
        patterns: list[str] = []
        for name in self._crud.list(_POLICY_CLASS):
            patterns.extend(self.get(name).protected)
        return patterns

    def matching_pattern(self, cls: str, name: str, path: str) -> str | None:
        """Return the first pattern that protects ``cls:name:path``, else None."""
        target = f"{cls}:{name}:{path}"
        for pat in self.all_patterns():
            if fnmatch(target, pat):
                return pat
        return None

    def is_protected(self, cls: str, name: str, path: str) -> bool:
        return self.matching_pattern(cls, name, path) is not None

    def enforce(self, cls: str, name: str, path: str, *, override: bool = False) -> None:
        """Raise ProtectedVarError if the var is protected and no override is held."""
        if override:
            return
        pat = self.matching_pattern(cls, name, path)
        if pat is not None:
            raise ProtectedVarError(cls, name, path, pat)
