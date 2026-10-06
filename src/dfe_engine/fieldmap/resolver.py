"""Field map resolver -- two-tier merge of default + source-specific maps.

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

from collections.abc import Collection
from typing import TYPE_CHECKING

from dfe_engine.fieldmap.models import FieldMap

if TYPE_CHECKING:
    from dfe_engine.fieldmap.registry import FieldMapRegistry


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
        Merged dict of standard_field -> dfe_column_name.
    """
    merged: dict[str, str] = {}

    if default_map is not None:
        merged.update(default_map.mappings)

    if source_map is not None:
        merged.update(source_map.mappings)

    return merged


def resolve_registry_mappings(
    registry: FieldMapRegistry,
    standard: str,
    source_name: str | None = None,
    *,
    field_map: str | None = None,
) -> dict[str, str]:
    """Resolve the two-tier registry mappings for a standard + source.

    Convenience over :func:`resolve_field_map`: loads the default and
    source-specific maps from a FieldMapRegistry (missing maps are fine)
    and merges them. Returns an empty dict when the standard has no maps.

    ``field_map`` is a SourceView pin: it names the registry map that forms
    the source-specific layer INSTEAD of the ``source_name`` convention -
    either ``"{standard}/{name}"`` (the leading segment must match
    *standard*) or a bare ``"{name}"``. The standard's ``_default`` map
    stays the base either way.

    Raises:
        FieldMapError: *field_map* declares a standard other than *standard*.
    """
    from dfe_engine.fieldmap.registry import FieldMapError, FieldMapNotFoundError

    # A field_map pin replaces the source-name convention for the override
    # layer, so a view can share one named map across many sources.
    override_name = source_name
    if field_map:
        pin = field_map
        if "/" in pin:
            pin_standard, _, pin = pin.partition("/")
            if pin_standard != standard:
                raise FieldMapError(
                    f"field_map {field_map!r} pins standard {pin_standard!r} "
                    f"but the view's standard is {standard!r}"
                )
        override_name = pin

    default_map: FieldMap | None = None
    source_map: FieldMap | None = None

    try:
        default_map = registry.get_map(standard)
    except FieldMapNotFoundError:
        pass

    if override_name:
        try:
            source_map = registry.get_map(standard, override_name)
        except FieldMapNotFoundError:
            pass

    return resolve_field_map(default_map, source_map)


def split_by_columns(
    mappings: dict[str, str],
    columns: Collection[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split *mappings* by whether each target column exists on the table.

    ClickHouse refuses a whole view (code 47) when any one of its columns is
    missing, and a default map names far more columns than any single table holds,
    so a view is rendered from the usable part only.

    Args:
        mappings: Resolved standard_field -> column_name mappings.
        columns: Column names the view's table carries.

    Returns:
        ``(usable, dropped)``: the mappings whose target is in *columns*, and the rest.
    """
    available = set(columns)
    usable: dict[str, str] = {}
    dropped: dict[str, str] = {}
    for standard_field, column_name in mappings.items():
        target = usable if column_name in available else dropped
        target[standard_field] = column_name
    return usable, dropped


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
