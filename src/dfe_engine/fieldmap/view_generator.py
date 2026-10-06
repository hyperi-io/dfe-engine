"""View generator -- produces ClickHouse view DDL from resolved field maps.

Generates CREATE VIEW statements for each standard × source combination.
View naming convention: ``{table_name}_{standard}``
(e.g. ``windows_audit_sigma``, ``windows_audit_ecs``).

Usage:
    from dfe_engine.fieldmap.view_generator import ViewGenerator

    gen = ViewGenerator(field_map_registry)
    ddl = gen.generate_view("sigma", "windows_audit", "windows_audit")
    all_ddls = gen.generate_views_for_source("windows_audit", "windows_audit")
"""

from collections.abc import Collection

from scalo.logger import logger

from dfe_engine.fieldmap.models import FieldMap
from dfe_engine.fieldmap.registry import FieldMapNotFoundError, FieldMapRegistry
from dfe_engine.fieldmap.resolver import resolve_field_map, split_by_columns
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.source.type_registry import TypeRegistry


def usable_view_mappings(
    mappings: dict[str, str],
    columns: Collection[str] | None,
    *,
    view_name: str,
) -> dict[str, str]:
    """The part of *mappings* a view over a table with *columns* can carry.

    ``columns=None`` means the table is unknown and every mapping is kept. A
    mapping whose target column is absent is dropped and logged, because one such
    column makes ClickHouse refuse the whole view.

    Args:
        mappings: Resolved standard_field -> column_name mappings.
        columns: Column names the view's table carries, or None when unknown.
        view_name: The view the mappings are for, named in the log line.

    Returns:
        The usable mappings; empty when none of them target a column on the table.
    """
    if columns is None:
        return mappings
    usable, dropped = split_by_columns(mappings, columns)
    if dropped:
        logger.debug(
            f"View {view_name!r}: dropped {len(dropped)} of {len(mappings)} mappings "
            f"whose column is not on the table: {sorted(dropped.items())}"
        )
    if mappings and not usable:
        logger.info(
            f"View {view_name!r} not rendered: none of its {len(mappings)} mappings "
            "target a column on the table"
        )
    return usable


class ViewGenerator:
    """Generates ClickHouse views from field map definitions.

    Combines the FieldMapRegistry (for loading maps) with the
    DDLGenerator (for producing SQL) to create standard views.
    """

    def __init__(
        self,
        field_map_registry: FieldMapRegistry,
        type_registry: TypeRegistry | None = None,
    ) -> None:
        self._fm_registry = field_map_registry
        self._ddl_gen = DDLGenerator(type_registry or TypeRegistry.default())

    def _resolve_map(self, standard: str, source_name: str | None) -> dict[str, str]:
        """Load and resolve a field map for a standard + source.

        Returns the merged mappings dict (default + source-specific).
        Returns empty dict if no maps exist for this standard.
        """
        default_map: FieldMap | None = None
        source_map: FieldMap | None = None

        try:
            default_map = self._fm_registry.get_map(standard)
        except FieldMapNotFoundError:
            pass

        if source_name:
            try:
                source_map = self._fm_registry.get_map(standard, source_name)
            except FieldMapNotFoundError:
                pass

        return resolve_field_map(default_map, source_map)

    def generate_view(
        self,
        standard: str,
        source_name: str,
        table_name: str,
        config: DDLConfig | None = None,
        columns: Collection[str] | None = None,
    ) -> str | None:
        """Generate a single view DDL for a standard × source.

        Args:
            standard: Standard name (e.g. "sigma", "ecs", "cim").
            source_name: Source name for source-specific overrides.
            table_name: ClickHouse base table name.
            config: DDL configuration.
            columns: Column names on *table_name*. When given, a mapping whose
                column is absent is left out of the view.

        Returns:
            CREATE VIEW DDL string, or None if no usable mappings resolved.
        """
        mappings = usable_view_mappings(
            self._resolve_map(standard, source_name),
            columns,
            view_name=f"{table_name}_{standard}",
        )
        if not mappings:
            return None

        return self._ddl_gen.generate_view(table_name, mappings, standard, config)

    def generate_views_for_source(
        self,
        source_name: str,
        table_name: str,
        standards: list[str] | None = None,
        config: DDLConfig | None = None,
        columns: Collection[str] | None = None,
    ) -> dict[str, str]:
        """Generate view DDLs for all standards that have maps for a source.

        Args:
            source_name: Source name.
            table_name: ClickHouse base table name.
            standards: Standards to generate views for. If None, discovers
                from registry (all standards with either a default or
                source-specific map).
            config: DDL configuration.
            columns: Column names on *table_name*; see :meth:`generate_view`.

        Returns:
            Dict of standard -> view DDL string.
        """
        if standards is None:
            standards = self._discover_standards(source_name)

        views: dict[str, str] = {}
        for standard in standards:
            ddl = self.generate_view(standard, source_name, table_name, config, columns)
            if ddl:
                views[standard] = ddl

        return views

    def _discover_standards(self, source_name: str) -> list[str]:
        """Discover which standards have field maps (default or source-specific).

        Returns a sorted list of standard names.
        """
        standards: set[str] = set()
        for entry in self._fm_registry.list_maps():
            std = entry["standard"]
            # Include if there's a default map or a source-specific map
            if entry["source"] is None or entry["source"] == source_name:
                standards.add(std)

        return sorted(standards)
