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

SEARCHABLE_COLUMN_ALIASES: dict[str, str] = {"type_filter": "type"}


def _normalize_searchable_columns(searchable_columns: list[str] | None) -> list[str] | None:
    if not searchable_columns:
        return None
    normalized: list[str] = []
    for field in searchable_columns:
        canonical = SEARCHABLE_COLUMN_ALIASES.get(field, field)
        if canonical not in COLUMN_FILTER_FIELDS:
            allowed = ", ".join((*COLUMN_FILTER_FIELDS, "type_filter"))
            raise ValueError(f"Invalid searchable_columns entry '{field}'; allowed: {allowed}")
        normalized.append(canonical)
    return normalized


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


def _matched_search_fields(
    col: SchemaColumn, query: str, searchable_columns: list[str]
) -> list[str]:
    q = query.casefold()
    return [
        field
        for field in searchable_columns
        if q in _field_as_search_text(_field_value(col, field)).casefold()
    ]


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
    searchable_columns: list[str] | None = None,
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

    explicit_searchable = _normalize_searchable_columns(searchable_columns)
    search_fields = (
        list(COLUMN_FILTER_FIELDS) if explicit_searchable is None else explicit_searchable
    )

    if search:
        filtered: list[SchemaColumn] = []
        for col in result:
            matched = _matched_search_fields(col, search, search_fields)
            if matched:
                filtered.append(col.model_copy(update={"matched_searchable": matched}))
        return filtered

    return [col.model_copy(update={"matched_searchable": []}) for col in result]
