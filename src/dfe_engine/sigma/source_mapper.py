"""Source-based Sigma field mapper.

Bridges the Sigma module to the Source model. Loads field mappings
from the source's sigma ``SourceView`` entry and generates Sigma views
via DDLGenerator.

Mapping precedence (locked): the registry field maps (default +
source-specific) form the base, and the sigma view's inline
``custom_mappings`` WIN per-key over them.

Usage:
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    mapper = SigmaSourceMapper(source_registry)
    mappings = mapper.get_field_mappings("windows_audit")
    view_ddl = mapper.generate_sigma_view("windows_audit")
"""

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.fieldmap.registry import FieldMapError
from dfe_engine.fieldmap.view_generator import usable_view_mappings
from dfe_engine.gitcrud import ResourceNotFoundError
from dfe_engine.schema.schema_builder_v2 import SchemaBuildError
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.schema.schema_loader import SchemaLoadError
from dfe_engine.sigma.views import SigmaViewError, build_sigma_view_ddl
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.source.type_registry import TypeRegistry

if TYPE_CHECKING:
    from dfe_engine.fieldmap.registry import FieldMapRegistry
    from dfe_engine.sigma.views import SigmaViewStore


class SigmaSourceMapper:
    """Maps Source definitions to Sigma field mappings.

    Uses SourceRegistry to look up sources and their Sigma config.

    When a FieldMapRegistry is provided, field mappings are resolved
    via two-tier resolution (default + source-specific overrides) as the
    base. The sigma view's inline ``custom_mappings`` then WIN per-key
    over the registry result (locked precedence).

    When a SigmaViewStore is provided, a stored CRUD view definition for a
    source WINS over the static field maps in view generation: it can expose
    columns DERIVED FROM the source's ``_json`` payload, which the field maps
    (real-column -> Sigma-field only) cannot. Generation falls back to the field
    maps when a source has no stored definition.

    When ``columns_for`` is provided, a field-map view keeps only the mappings
    whose column the source's table carries, the same rule the source deploy
    applies, so the rendered DDL is one ClickHouse accepts.
    """

    def __init__(
        self,
        source_registry: SourceRegistry,
        registry: TypeRegistry | None = None,
        field_map_registry: FieldMapRegistry | None = None,
        view_store: SigmaViewStore | None = None,
        columns_for: Callable[[Source], Collection[str]] | None = None,
    ) -> None:
        """Bind the mapper to the registries its mappings and views come from.

        Args:
            source_registry: Where sources are looked up.
            registry: Type registry, the default one when omitted.
            field_map_registry: Registry field maps, the base of every mapping.
            view_store: Stored sigma view definitions, which win over the field maps.
            columns_for: The column names of a source's table; None renders every
                mapping whatever the table holds.
        """
        self._source_registry = source_registry
        self._type_registry = registry or TypeRegistry.default()
        self._ddl_gen = DDLGenerator(self._type_registry)
        self._field_map_registry = field_map_registry
        self._view_store = view_store
        self._columns_for = columns_for

    def get_source(self, source_name: str) -> Source:
        """Get a Source by name from the registry."""
        return self._source_registry.get_source(source_name)

    def get_field_mappings(self, source_name: str) -> dict[str, str]:
        """Get Sigma field -> column name mappings for a source.

        Resolution: the FieldMapRegistry (two-tier: default +
        source-specific) is the base, and the sigma view's inline
        ``custom_mappings`` WIN per-key over it (locked precedence).

        Args:
            source_name: Source name (e.g. 'windows_audit').

        Returns:
            Dict mapping Sigma field names to ClickHouse column names.
            Empty dict if no mappings found.

        Raises:
            SourceNotFoundError: Source not found in registry.
        """
        # Validate source exists (raises SourceNotFoundError if missing)
        source = self._source_registry.get_source(source_name)
        return self._get_mappings_for_source(source)

    def generate_sigma_view(
        self,
        source_name: str,
        db: str = "{db}",
    ) -> str | None:
        """Generate a Sigma view DDL for a source.

        Resolution order:
        1. A stored CRUD view definition (SigmaViewStore) - can extract
           JSON-derived columns from ``_json``.
        2. Static field maps: the registry base with the sigma view's inline
           ``custom_mappings`` winning per-key.

        Returns None if the source has neither a stored definition nor any
        usable field mapping.
        """
        source = self._source_registry.get_source(source_name)
        return self._generate_for_source(source, db)

    def generate_all_sigma_views(
        self,
        db: str = "{db}",
        enabled_only: bool = True,
    ) -> dict[str, str]:
        """Generate Sigma view DDLs for all sources with a view.

        A source contributes a view when it has a stored view definition OR
        non-empty field mappings. A source whose schema, field map or stored view
        cannot render is logged and left out, so one broken source does not
        blank the views of every other.

        Returns:
            Dict mapping source_name -> Sigma view DDL string.
        """
        views: dict[str, str] = {}
        for source in self._source_registry.get_all_sources(enabled_only=enabled_only):
            try:
                ddl = self._generate_for_source(source, db)
            except (SchemaBuildError, SchemaLoadError, FieldMapError, SigmaViewError) as exc:
                logger.warning(f"sigma view for source {source.source!r} not rendered: {exc}")
                continue
            if ddl:
                views[source.source] = ddl

        return views

    def _generate_for_source(self, source: Source, db: str) -> str | None:
        """Render a source's Sigma view DDL (stored definition wins over field maps).

        A stored ``SigmaViewStore`` definition is the SSoT when present (it alone
        can declare JSON-derived columns); otherwise fall back to the static field
        maps, returning None when a source has neither.
        """
        if self._view_store is not None:
            try:
                definition = self._view_store.get(source.source)
            except ResourceNotFoundError:
                definition = None
            if definition is not None:
                return build_sigma_view_ddl(definition, db=db, table_name=source.table_name)

        columns = self._columns_for(source) if self._columns_for is not None else None
        mappings = usable_view_mappings(
            self._get_mappings_for_source(source),
            columns,
            view_name=f"{source.table_name}_sigma",
        )
        if not mappings:
            return None
        return self._ddl_gen.generate_sigma_view(source.table_name, mappings, DDLConfig(db=db))

    def _get_mappings_for_source(self, source: Source) -> dict[str, str]:
        """Get Sigma mappings for a source (registry base, inline wins).

        Like get_field_mappings() but takes a Source directly (avoids
        repeated registry lookups in batch operations). PRECEDENCE (locked):
        the registry maps are the base and the sigma view's inline
        ``custom_mappings`` override per-key, so the two mapping sources
        cannot silently disagree. A sigma view's ``field_map`` pin replaces
        the source-name convention for the registry override layer.
        """
        from dfe_engine.fieldmap.resolver import resolve_registry_mappings

        sigma_view = source.view_for("sigma")
        mappings: dict[str, str] = {}
        if self._field_map_registry:
            mappings.update(
                resolve_registry_mappings(
                    self._field_map_registry,
                    "sigma",
                    source.source,
                    field_map=sigma_view.field_map if sigma_view else None,
                )
            )

        if sigma_view is not None:
            mappings.update(sigma_view.custom_mappings)
        return mappings

    def get_sources_for_logsource(
        self,
        product: str | None = None,
        category: str | None = None,
        service: str | None = None,
    ) -> list[Source]:
        """Find sources matching a Sigma logsource specification.

        Maps Sigma logsource fields to the source's sigma view entry:
        - product  -> sigma view taxonomy
        - category -> sigma view category (None on the source = matches any)
        - service  -> sigma view service  (None on the source = matches any)

        A source with NO sigma view has no logsource binding and never matches.
        A source is bound only when the product matches AND every logsource facet
        the source DECLARES also matches - so two same-product sources (e.g.
        windows_audit vs windows_sysmon) no longer both receive a sysmon-only
        rule. A source with no declared category/service keeps product-only
        behaviour.

        Args:
            product: Sigma product (e.g. 'windows', 'linux').
            category: Sigma category (e.g. 'process_creation').
            service: Sigma service (e.g. 'sysmon').

        Returns:
            List of matching Source models.
        """
        matches: list[Source] = []
        for source in self._source_registry.get_all_sources(enabled_only=True):
            sig = source.view_for("sigma")
            if sig is None or not (product and sig.taxonomy):
                continue
            if sig.taxonomy.lower() != product.lower():
                continue
            # Narrow by any facet the source declares; a None facet matches any.
            if category and sig.category and sig.category.lower() != category.lower():
                continue
            if service and sig.service and sig.service.lower() != service.lower():
                continue
            matches.append(source)

        return matches
