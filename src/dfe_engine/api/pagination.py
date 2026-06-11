"""Pagination models and helpers for API list endpoints.

PaginatedResponse[T] is the standard envelope. Compatible with TanStack Query
useInfiniteQuery: UI reads ``next_page`` from ``getNextPageParam``.
"""

from __future__ import annotations

from typing import Annotated, Any, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, Field, computed_field

T = TypeVar("T")


def validate_per_page(value: int) -> int:
    """Accept -1 (all items) or a page size in [1, 100]."""
    if value == -1 or 1 <= value <= 100:
        return value
    msg = "per_page must be -1 or between 1 and 100"
    raise ValueError(msg)


PerPageParam = Annotated[int, AfterValidator(validate_per_page)]


class PaginationParams(BaseModel):
    """FastAPI dependency for pagination query parameters."""

    page: int = Field(1, ge=1, description="Page number (1-based)")
    per_page: PerPageParam = Field(
        25,
        description="Items per page; use -1 to return all items (ignores page)",
    )


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
        if self.per_page == -1:
            return 1
        if self.per_page <= 0:
            return 0
        return max(1, -(-self.total // self.per_page))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_page(self) -> int | None:
        if self.per_page == -1:
            return None
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

        When ``per_page`` is ``-1``, returns every item in a single page.
        """
        total = len(all_items)
        if per_page == -1:
            return cls(items=list(all_items), total=total, page=1, per_page=-1)
        per_page = validate_per_page(per_page)
        start = (page - 1) * per_page
        end = start + per_page
        items = all_items[start:end]
        return cls(
            items=items,
            total=total,
            page=page,
            per_page=per_page,
        )


# ── Helpers ──────────────────────────────────────────────────


def schema_path_top_level(path: str) -> str:
    """First path segment of a schema registry key (e.g. ``meta/foo`` → ``meta``)."""
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else ""


def apply_schema_type_filter(
    items: list[dict[str, Any]], schema_types: list[str] | None
) -> list[dict[str, Any]]:
    """Keep items whose path top-level segment is in ``schema_types``."""
    if not schema_types:
        return items
    allowed = set(schema_types)
    return [
        item
        for item in items
        if schema_path_top_level(str(item.get("path", ""))) in allowed
    ]


def apply_search(
    items: list[dict[str, Any]], query: str | None, fields: list[str]
) -> list[dict[str, Any]]:
    """Filter items where any field contains query text (case-insensitive)."""
    if not query:
        return items
    q = query.lower()
    return [item for item in items if any(q in str(item.get(f, "")).lower() for f in fields)]


def apply_sort(
    items: list[dict[str, Any]], sort_by: str | None, sort_order: str = "asc"
) -> list[dict[str, Any]]:
    """Sort list of dicts by key. None-safe."""
    if not sort_by:
        return items
    descending = sort_order in ("desc", "descend")
    return sorted(items, key=lambda x: x.get(sort_by) or "", reverse=descending)
