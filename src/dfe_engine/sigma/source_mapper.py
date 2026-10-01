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

from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.gitcrud import ResourceNotFoundError
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.sigma.views import build_sigma_view_ddl
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.source.type_registry import TypeRegistry

if TYPE_CHECKING:
    from dfe_engine.fieldmap.registry import FieldMapRegistry
    from dfe_engine.sigma.views import SigmaViewDefinition, SigmaViewStore


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
    """

    def __init__(
        self,
        source_registry: SourceRegistry,
        registry: TypeRegistry | None = None,
        field_map_registry: FieldMapRegistry | None = None,
        view_store: SigmaViewStore | None = None,
    ) -> None:
        self._source_registry = source_registry
        self._type_registry = registry or TypeRegistry.default()
        self._ddl_gen = DDLGenerator(self._type_registry)
        self._field_map_registry = field_map_registry
        self._view_store = view_store

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

    def get_schema_metadata(self, source_name: str) -> dict[str, dict[str, str | list[str] | None]]:
        """Get schema column metadata for a source.

        Returns a dict keyed by column name with type info:
            {"user_name": {"type": "string", "use_case": "dimension", "attribute": [...]}}

        Returns an empty dict when the source's schema fails to build.
        """
        return self._schema_metadata_for_source(self._source_registry.get_source(source_name))

    def get_sigma_field_metadata(
        self, source_name: str
    ) -> dict[str, dict[str, str | list[str] | None]]:
        """Get schema column metadata keyed by the Sigma field the source's view exposes.

        A sigma rule's WHERE clause runs over the ``{source}_sigma`` view, whose
        columns carry Sigma field names, so the backend looks metadata up by those
        names. Each field the view aliases to a real column takes that column's
        metadata. A field the view extracts from ``_json``, CASTs, or does not
        alias at all gets none, and so keeps the ILIKE match.

        Args:
            source_name: Source name (e.g. 'windows-audit').

        Returns:
            Dict mapping Sigma field names to their column's metadata. Empty when
            the view aliases no real column or the source's schema fails to build.

        Raises:
            SourceNotFoundError: Source not found in registry.
            FieldMapError: The sigma view pins a field map of another standard.
        """
        source = self._source_registry.get_source(source_name)
        aliases = self._view_column_aliases(source)
        if not aliases:
            return {}
        column_metadata = self._schema_metadata_for_source(source)
        return {
            sigma_field: column_metadata[column]
            for sigma_field, column in aliases.items()
            if column in column_metadata
        }

    def _schema_metadata_for_source(
        self, source: Source
    ) -> dict[str, dict[str, str | list[str] | None]]:
        """Column metadata from the source's composed schema; empty if the build fails."""
        builder = SchemaBuilderV2(
            registry=self._type_registry,
        )

        # Build to get composed columns (profile + meta + derived + additional)
        try:
            result = builder.build(source)
        except Exception as e:
            logger.warning(f"Failed to build schema for source '{source.source}': {e}")
            return {}

        metadata: dict[str, dict[str, str | list[str] | None]] = {}
        for col in result.columns:
            metadata[col.name] = {
                "type": col.type,
                "use_case": col.use_case,
                "attribute": col.attribute,
            }

        return metadata

    def _view_column_aliases(self, source: Source) -> dict[str, str]:
        """Sigma field -> real column for every plain alias the source's sigma view renders.

        Follows the same resolution as view generation: a stored definition wins,
        and contributes only its uncast ``source_column`` entries, since a JSON
        extraction or a CAST is not the expression the column's index covers.
        """
        definition = self._stored_definition(source)
        if definition is None:
            return self._get_mappings_for_source(source)
        return {
            column.sigma_field: column.source_column
            for column in definition.columns
            if column.source_column and not column.type
        }

    def _stored_definition(self, source: Source) -> SigmaViewDefinition | None:
        """The source's stored sigma view definition, or None when there is none."""
        if self._view_store is None:
            return None
        try:
            return self._view_store.get(source.source)
        except ResourceNotFoundError:
            return None

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
        field mappings.
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
        non-empty field mappings.

        Returns:
            Dict mapping source_name -> Sigma view DDL string.
        """
        views: dict[str, str] = {}
        for source in self._source_registry.get_all_sources(enabled_only=enabled_only):
            ddl = self._generate_for_source(source, db)
            if ddl:
                views[source.source] = ddl

        return views

    def _generate_for_source(self, source: Source, db: str) -> str | None:
        """Render a source's Sigma view DDL (stored definition wins over field maps).

        A stored ``SigmaViewStore`` definition is the SSoT when present (it alone
        can declare JSON-derived columns); otherwise fall back to the static field
        maps, returning None when a source has neither.
        """
        definition = self._stored_definition(source)
        if definition is not None:
            return build_sigma_view_ddl(definition, db=db, table_name=source.table_name)

        mappings = self._get_mappings_for_source(source)
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
