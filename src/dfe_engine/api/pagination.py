"""Pagination models and helpers for API list endpoints.

PaginatedResponse[T] is the standard envelope. Compatible with TanStack Query
useInfiniteQuery: UI reads ``next_page`` from ``getNextPageParam``.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field, computed_field

T = TypeVar("T")


class PaginationParams:
    """FastAPI dependency for pagination query parameters."""

    def __init__(
        self,
        page: int = Query(1, ge=1, description="Page number (1-based)"),
        per_page: int = Query(25, ge=1, le=100, description="Items per page"),
    ):
        self.page = page
        self.per_page = per_page


class SortOrder(str):
    """Sort direction. Accepts both 'asc'/'desc' and Ant Design 'ascend'/'descend'."""

    @property
    def is_descending(self) -> bool:
        return self in ("desc", "descend")


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response envelope.

    Designed for TanStack Query ``useInfiniteQuery``::

        getNextPageParam: (lastPage) => lastPage.next_page ?? undefined
    """

    items: list[T]
    total: int = Field(description="Total matching items across all pages")
    page: int = Field(description="Current page number (1-based)")
    per_page: int = Field(description="Items per page")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_pages(self) -> int:
        if self.per_page <= 0:
            return 0
        return max(1, -(-self.total // self.per_page))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_page(self) -> int | None:
        if self.page < self.total_pages:
            return self.page + 1
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def prev_page(self) -> int | None:
        if self.page > 1:
            return self.page - 1
        return None

    @classmethod
    def from_list(
        cls,
        all_items: list[T],
        page: int,
        per_page: int,
    ) -> PaginatedResponse[T]:
        """Create paginated response from an in-memory list.

        Suitable for YAML-backed registries where data fits in memory.
        """
        total = len(all_items)
        start = (page - 1) * per_page
        end = start + per_page
        items = all_items[start:end]
        return cls(items=items, total=total, page=page, per_page=per_page)


# ── Helpers ──────────────────────────────────────────────────


def apply_search(
    items: list[dict[str, Any]], query: str | None, fields: list[str]
) -> list[dict[str, Any]]:
    """Filter items where any field contains query text (case-insensitive)."""
    if not query:
        return items
    q = query.lower()
    return [
        item
        for item in items
        if any(q in str(item.get(f, "")).lower() for f in fields)
    ]


def apply_sort(
    items: list[dict[str, Any]], sort_by: str | None, sort_order: str = "asc"
) -> list[dict[str, Any]]:
    """Sort list of dicts by key. None-safe."""
    if not sort_by:
        return items
    descending = sort_order in ("desc", "descend")
    return sorted(items, key=lambda x: x.get(sort_by) or "", reverse=descending)
