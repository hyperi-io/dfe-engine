"""Filter helpers for meta-schema column lists (API GET)."""

from __future__ import annotations

from typing import Any

from dfe_engine.schema.models import SchemaColumn

COLUMN_FILTER_FIELDS: tuple[str, ...] = (
    "name",
    "type",
    "use_case",
    "expr",
    "comment",
    "attribute",
)


def _field_value(col: SchemaColumn, field: str) -> Any:
    return getattr(col, field)


def _field_as_search_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


def _matches_field_filter(col: SchemaColumn, field: str, filter_value: str) -> bool:
    text = _field_as_search_text(_field_value(col, field))
    return filter_value.casefold() in text.casefold()


def filter_columns(
    columns: list[SchemaColumn],
    *,
    search: str | None = None,
    name: str | None = None,
    type: str | None = None,
    use_case: str | None = None,
    expr: str | None = None,
    comment: str | None = None,
    attribute: str | None = None,
) -> list[SchemaColumn]:
    """Filter columns; all filters are case-insensitive substring matches."""
    field_filters = {
        "name": name,
        "type": type,
        "use_case": use_case,
        "expr": expr,
        "comment": comment,
        "attribute": attribute,
    }
    result = columns
    for field, filt in field_filters.items():
        if filt is None:
            continue
        result = [c for c in result if _matches_field_filter(c, field, filt)]

    if search:
        q = search.casefold()
        result = [
            c
            for c in result
            if any(
                q in _field_as_search_text(_field_value(c, f)).casefold()
                for f in COLUMN_FILTER_FIELDS
            )
        ]
    return result
