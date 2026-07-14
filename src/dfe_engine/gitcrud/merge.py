#  Project:      dfe-engine
#  File:         gitcrud/merge.py
#  Purpose:      Structured 3-way merge for gitcrud resources (drift reconciliation)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Structured 3-way merge for gitcrud resource docs.

The reconciliation primitive behind "clone a read-only hyperi object, amend it, then
pull a NEWER upstream": ``three_way_merge(base, ours, theirs)`` where
``base`` = the upstream you branched from, ``ours`` = your amended copy,
``theirs`` = the newer upstream. It also works between ANY two objects given a common
``base`` - not just hyperi updates.

Because gitcrud objects are STRUCTURED YAML (dicts/lists), the merge is per-FIELD, not
line-based - so it never trips on YAML reordering/indentation the way ``git merge-file``
would, and it needs no external package. Per key: if ours and theirs agree, take it; if
only one side changed it from base, take that side; if both changed it differently,
recurse into dicts, else record a CONFLICT (keeping ours) for a human to resolve. Lists
are treated atomically (divergent list edits conflict) - reliable element-identity list
merging is a deliberate later enhancement.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Sentinel for "this key is absent" - lets add/delete be a normal case in the merge.
_MISSING: Any = object()


class MergeResult(BaseModel):
    """The merged doc plus the field paths where ours and theirs conflicted."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    merged: dict[str, Any]
    conflicts: list[str] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True when the merge had no conflicts (safe to apply automatically)."""
        return not self.conflicts


def three_way_merge(
    base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
) -> MergeResult:
    """Merge ``ours`` and ``theirs`` against their common ancestor ``base``.

    Returns the merged doc and the conflicting field paths (dotted). On a conflict the
    merged value keeps OURS, so applying a conflicted result is safe-by-default (your
    edits win until you resolve).
    """
    conflicts: list[str] = []
    merged = _merge_dict(base or {}, ours or {}, theirs or {}, [], conflicts)
    return MergeResult(merged=merged, conflicts=conflicts)


def _merge_value(b: Any, o: Any, t: Any, path: list[str], conflicts: list[str]) -> Any:
    if o == t:  # both sides agree (incl. both added the same, or both absent)
        return o
    if o == b:  # we did not change it -> take theirs (which may be _MISSING = delete)
        return t
    if t == b:  # theirs did not change it -> take ours
        return o
    # all three differ - recurse if all dicts, else it is a genuine conflict
    if isinstance(b, dict) and isinstance(o, dict) and isinstance(t, dict):
        return _merge_dict(b, o, t, path, conflicts)
    conflicts.append(".".join(path) or "<root>")
    return o  # keep ours; the human resolves


def _merge_dict(
    b: dict[str, Any], o: dict[str, Any], t: dict[str, Any], path: list[str], conflicts: list[str]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(set(b) | set(o) | set(t)):
        merged = _merge_value(
            b.get(key, _MISSING),
            o.get(key, _MISSING),
            t.get(key, _MISSING),
            [*path, str(key)],
            conflicts,
        )
        if merged is not _MISSING:  # _MISSING = the key was deleted in the merge
            result[key] = merged
    return result
