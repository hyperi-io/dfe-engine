"""Source-based Sigma field mapper — replaces PG + CSV field mapping.

Bridges the Sigma module to the Source model. Loads field mappings
from Source.sigma config and generates Sigma views via DDLGenerator.

Usage:
    from dfe_engine.sigma.source_mapper import SigmaSourceMapper

    mapper = SigmaSourceMapper(source_registry)
    mappings = mapper.get_field_mappings("windows_audit")
    view_ddl = mapper.generate_sigma_view("windows_audit")
"""

from __future__ import annotations

from hyperi_pylib.logger import logger

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceRegistry
from dfe_engine.source.type_registry import TypeRegistry


class SigmaSourceMapper:
    """Maps Source definitions to Sigma field mappings.

    Replaces the PostgreSQL/CSV-based field_mapping_service.py.
    Uses SourceRegistry to look up sources and their Sigma config.
    """

    def __init__(
        self,
        source_registry: SourceRegistry,
        registry: TypeRegistry | None = None,
    ) -> None:
        self._source_registry = source_registry
        self._type_registry = registry or TypeRegistry.default()
        self._ddl_gen = DDLGenerator(self._type_registry)

    def get_source(self, source_name: str) -> Source:
        """Get a Source by name from the registry."""
        return self._source_registry.get_source(source_name)

    def get_field_mappings(self, source_name: str) -> dict[str, str]:
        """Get Sigma field → column name mappings for a source.

        Args:
            source_name: Source name (e.g. 'windows_audit').

        Returns:
            Dict mapping Sigma field names to ClickHouse column names.
            Empty dict if no sigma config.

        Raises:
            SourceNotFoundError: Source not found in registry.
        """
        source = self._source_registry.get_source(source_name)

        if not source.sigma:
            return {}

        return dict(source.sigma.custom_mappings)

    def get_schema_metadata(
        self, source_name: str
    ) -> dict[str, dict[str, str | list[str] | None]]:
        """Get schema column metadata for a source.

        Returns a dict keyed by column name with type info:
            {"user_name": {"type": "string", "use_case": "dimension", "attribute": [...]}}

        Replaces the old CSV/PG-based _read_schema_metadata().
        """
        source = self._source_registry.get_source(source_name)

        builder = SchemaBuilderV2(
            registry=self._type_registry,
        )

        # Build to get composed columns (profile + meta + derived + additional)
        try:
            result = builder.build(source)
        except Exception as e:
            logger.warning(
                f"Failed to build schema for source '{source_name}': {e}"
            )
            return {}

        metadata: dict[str, dict[str, str | list[str] | None]] = {}
        for col in result.columns:
            metadata[col.name] = {
                "type": col.type,
                "use_case": col.use_case,
                "attribute": col.attribute,
            }

        return metadata

    def generate_sigma_view(
        self,
        source_name: str,
        db: str = "{db}",
    ) -> str | None:
        """Generate a Sigma view DDL for a source.

        Returns None if the source has no sigma config.
        """
        source = self._source_registry.get_source(source_name)

        if not source.sigma or not source.sigma.custom_mappings:
            return None

        config = DDLConfig(db=db)
        return self._ddl_gen.generate_sigma_view(
            source.table_name, source.sigma.custom_mappings, config
        )

    def generate_all_sigma_views(
        self,
        db: str = "{db}",
        enabled_only: bool = True,
    ) -> dict[str, str]:
        """Generate Sigma view DDLs for all sources with sigma config.

        Returns:
            Dict mapping source_name → Sigma view DDL string.
        """
        views: dict[str, str] = {}
        for source in self._source_registry.get_all_sources(
            enabled_only=enabled_only
        ):
            if source.sigma and source.sigma.custom_mappings:
                config = DDLConfig(db=db)
                ddl = self._ddl_gen.generate_sigma_view(
                    source.table_name, source.sigma.custom_mappings, config
                )
                views[source.source] = ddl

        return views

    def get_sources_for_logsource(
        self,
        product: str | None = None,
        category: str | None = None,
        service: str | None = None,
    ) -> list[Source]:
        """Find sources matching a Sigma logsource specification.

        Maps Sigma logsource fields to Source taxonomy:
        - product → source.sigma.taxonomy
        - category/service → matched by convention

        Args:
            product: Sigma product (e.g. 'windows', 'linux').
            category: Sigma category (e.g. 'process_creation').
            service: Sigma service (e.g. 'sysmon').

        Returns:
            List of matching Source models.
        """
        matches: list[Source] = []
        for source in self._source_registry.get_all_sources(enabled_only=True):
            if not source.sigma:
                continue

            # Match by taxonomy (product)
            if product and source.sigma.taxonomy:
                if source.sigma.taxonomy.lower() == product.lower():
                    matches.append(source)

        return matches
