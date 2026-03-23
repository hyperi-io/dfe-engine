"""Field map helpers — grouping and aggregation utilities."""

from __future__ import annotations

from typing import Literal, Protocol, TypeVar


class _GroupableSummary(Protocol):
    """Protocol for items groupable by standard or version."""

    standard: str
    version: str | None


T = TypeVar("T", bound=_GroupableSummary)


def group_summaries(
    summaries: list[T],
    group_by: Literal["standard", "version"],
    max_per_group: int = 10,
    sort_order: str = "asc",
) -> list[dict[str, str | int | list[T]]]:
    """Group summaries by the given field key. None/empty version maps to \"\".

    Limits each group to max_per_group items (-1 for all). Sorts within groups by sort_order.
    Returns a list of objects: [{"key": group_key, "items": [...], "total": N}, ...].
    """
    group: dict[str, list[T]] = {}
    for s in summaries:
        key = s.standard if group_by == "standard" else (s.version or "")
        if key not in group:
            group[key] = []
        group[key].append(s)

    descending = sort_order in ("desc", "descend")
    result: list[dict[str, str | int | list[T]]] = []
    for k, items in group.items():
        sort_key = "standard" if group_by == "version" else "version"
        sorted_items = sorted(
            items,
            key=lambda x: (getattr(x, sort_key) or ""),
            reverse=descending,
        )
        limited = sorted_items if max_per_group == -1 else sorted_items[:max_per_group]
        result.append({"key": k, "items": limited, "total": len(sorted_items)})
    return result
