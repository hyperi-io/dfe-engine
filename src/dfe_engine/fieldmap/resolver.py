"""Field map resolver — two-tier merge of default + source-specific maps.

Pure function approach: takes FieldMap objects as input, returns merged
mappings dict. No registry dependency.

Resolution priority (highest wins):
    1. Source-specific map overrides
    2. Default map for the standard
    3. Passthrough (field name used as-is if not mapped)

Usage:
    from dfe_engine.fieldmap.resolver import resolve_field_map, resolve_field

    merged = resolve_field_map(default_map=sigma_default, source_map=sigma_win)
    column = resolve_field("CommandLine", merged)
"""

from __future__ import annotations

from dfe_engine.fieldmap.models import FieldMap


def resolve_field_map(
    default_map: FieldMap | None = None,
    source_map: FieldMap | None = None,
) -> dict[str, str]:
    """Resolve a field map by merging default + source-specific maps.

    Priority: source_map > default_map.
    If both are None, returns an empty dict (all fields passthrough).

    Args:
        default_map: The _default map for the standard (base layer).
        source_map: Source-specific map (override layer).

    Returns:
        Merged dict of standard_field → dfe_column_name.
    """
    merged: dict[str, str] = {}

    if default_map is not None:
        merged.update(default_map.mappings)

    if source_map is not None:
        merged.update(source_map.mappings)

    return merged


def resolve_field(
    field_name: str,
    resolved_map: dict[str, str],
) -> str:
    """Resolve a single field name through a resolved map.

    Returns the mapped column name, or the original field name
    if no mapping exists (passthrough).

    Args:
        field_name: The standard field name to resolve.
        resolved_map: Pre-resolved mapping from resolve_field_map().

    Returns:
        DFE column name.
    """
    return resolved_map.get(field_name, field_name)
