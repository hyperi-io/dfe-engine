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

A write reaches every var on its own line of the tree. Setting or deleting a parent
map replaces each leaf below it, and setting below a scalar turns that scalar into a
map, so a path is protected when a pattern matches it, any of its ancestors, or any
path below it.
"""

import builtins
from collections.abc import Iterable
from fnmatch import fnmatchcase

from dfe_engine.gitcrud import GitCrud, flatten

from .models import ProtectedPolicy

_POLICY_CLASS = "policies"

# Where a child path starts, in the dot-path form ``flatten`` produces (``a.b[0].c``).
_CHILD_SEPARATORS = (".", "[")


class ProtectedVarError(PermissionError):
    """Raised when a protected var is written without the override grant."""

    def __init__(self, cls: str, name: str, path: str, pattern: str) -> None:
        super().__init__(f"{cls}:{name}:{path} is protected by policy pattern '{pattern}'")
        self.cls = cls
        self.name = name
        self.path = path
        self.pattern = pattern


def _glob_units(glob: str) -> list[str]:
    """Split an fnmatch glob into the units it matches in turn, each ``[...]`` set whole."""
    units: list[str] = []
    i = 0
    while i < len(glob):
        end = i + 1
        if glob[i] == "[":
            j = i + 1
            if j < len(glob) and glob[j] == "!":
                j += 1
            if j < len(glob) and glob[j] == "]":
                j += 1
            close = glob.find("]", j)
            # An unclosed bracket is a literal character, as fnmatch reads it.
            if close != -1:
                end = close + 1
        units.append(glob[i:end])
        i = end
    return units


def _matches_below(glob: str, path: str) -> bool:
    """Whether ``glob`` matches some path strictly below ``path``.

    A string starting with a given prefix can match the glob exactly when that prefix
    matches the glob cut at some unit boundary: a ``*`` absorbs whatever follows it,
    and every other unit matches one character.
    """
    units = _glob_units(glob)
    cuts = ["".join(units[:k]) for k in range(len(units) + 1)]
    return any(fnmatchcase(path + sep, cut) for sep in _CHILD_SEPARATORS for cut in cuts)


def _ancestors(path: str) -> list[str]:
    """Every proper ancestor of a dot-path: ``a.b[0].c`` gives ``a``, ``a.b`` and ``a.b[0]``."""
    return [path[:i] for i, ch in enumerate(path) if i > 0 and ch in _CHILD_SEPARATORS]


def _reaches(pattern: str, cls: str, name: str, path: str) -> bool:
    """Whether a set or delete at ``cls:name:path`` changes a var ``pattern`` locks."""
    if any(fnmatchcase(f"{cls}:{name}:{p}", pattern) for p in (path, *_ancestors(path))):
        return True
    scope = pattern.split(":", 2)
    if len(scope) != 3:
        return _matches_below(pattern, f"{cls}:{name}:{path}")
    # Matched per part, so a `*` in the name cannot run on into the path.
    cls_glob, name_glob, path_glob = scope
    return (
        fnmatchcase(cls, cls_glob)
        and fnmatchcase(name, name_glob)
        and _matches_below(path_glob, path)
    )


def _reaching_pattern(patterns: Iterable[str], cls: str, name: str, path: str) -> str | None:
    """The first pattern a write at ``cls:name:path`` reaches, else None."""
    return next((pat for pat in patterns if _reaches(pat, cls, name, path)), None)


class PolicyStore:
    """Loads protected-var policies from the gitops governance class via GitCrud."""

    def __init__(self, crud: GitCrud) -> None:
        self._crud = crud

    def list(self) -> builtins.list[str]:
        """Name every policy document in the deploy repo."""
        return self._crud.list(_POLICY_CLASS)

    def get(self, name: str) -> ProtectedPolicy:
        """Read one policy document."""
        return ProtectedPolicy.model_validate(self._crud.get(_POLICY_CLASS, name))

    def all_patterns(self) -> builtins.list[str]:
        """Aggregate every protected pattern across all policy docs."""
        patterns: builtins.list[str] = []
        for name in self._crud.list(_POLICY_CLASS):
            patterns.extend(self.get(name).protected)
        return patterns

    def matching_pattern(self, cls: str, name: str, path: str) -> str | None:
        """Return the first pattern a write at ``cls:name:path`` reaches, else None."""
        return _reaching_pattern(self.all_patterns(), cls, name, path)

    def is_protected(self, cls: str, name: str, path: str) -> bool:
        """Whether a write at ``cls:name:path`` needs the override grant."""
        return self.matching_pattern(cls, name, path) is not None

    def enforce(self, cls: str, name: str, path: str, *, override: bool = False) -> None:
        """Raise ProtectedVarError if the var is protected and no override is held."""
        if override:
            return
        pat = self.matching_pattern(cls, name, path)
        if pat is not None:
            raise ProtectedVarError(cls, name, path, pat)

    def enforce_document(
        self,
        cls: str,
        name: str,
        before: dict,
        after: dict,
        *,
        override: bool = False,
    ) -> bool:
        """Gate a whole-document write and report whether it touches a protected var.

        Every leaf of the document as written is checked, and so is every leaf the
        write drops: removing a locked key changes it as surely as setting it does.

        Args:
            cls: Resource class the document belongs to.
            name: Resource name within the class.
            before: The document as stored, empty when the resource is new.
            after: The document as it will be written.
            override: The caller holds the override grant.

        Returns:
            Whether any leaf kept, added or dropped is protected.

        Raises:
            ProtectedVarError: A leaf is protected and no override is held.
        """
        patterns = self.all_patterns()
        protected = False
        for path in flatten(after) | flatten(before):
            pat = _reaching_pattern(patterns, cls, name, path)
            if pat is None:
                continue
            if not override:
                raise ProtectedVarError(cls, name, path, pat)
            protected = True
        return protected
