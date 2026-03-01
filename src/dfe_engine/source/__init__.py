"""DFE Source model — the top-level data abstraction.

A Source represents a data stream entering the DFE platform. It contains
identity, schema, and optional fetcher/transform/rules/sigma configuration.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- Backed by DirectoryConfigStore from hyperi-pylib
- In-memory cache with background polling refresh
- Optional git-aware writes (auto-commit, branch management, push)
- Schema definitions use simplified primitives mapped via TypeRegistry

Usage:
    from dfe_engine.source import Source, SourceRegistry, TypeRegistry
    from dfe_engine.source import SchemaColumn, ResolvedType

    # Type registry
    registry = TypeRegistry.default()
    resolved = registry.resolve("string", attributes=["lowcardinality"])

    # Source management
    source_reg = SourceRegistry(sources_directory="/etc/dfe/sources")
    source = source_reg.get_source("filebeat")
"""

from dfe_engine.source.models import (
    FetcherAuth,
    SchemaColumn,
    Source,
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceSigma,
    SourceTransform,
)
from dfe_engine.source.registry import (
    SourceNotFoundError,
    SourceRegistry,
    SourceRegistryError,
    SourceValidationError,
)
from dfe_engine.source.type_registry import (
    InvalidAttributeError,
    InvalidChOverrideError,
    InvalidUseCaseError,
    ResolvedType,
    TypeRegistry,
    TypeRegistryError,
    UnknownPrimitiveError,
)

__all__ = [
    # Type Registry
    "InvalidAttributeError",
    "InvalidChOverrideError",
    "InvalidUseCaseError",
    "ResolvedType",
    "TypeRegistry",
    "TypeRegistryError",
    "UnknownPrimitiveError",
    # Models
    "FetcherAuth",
    "SchemaColumn",
    "Source",
    "SourceFetcher",
    "SourceHeader",
    "SourceMatch",
    "SourceSchema",
    "SourceSigma",
    "SourceTransform",
    # Registry
    "SourceNotFoundError",
    "SourceRegistry",
    "SourceRegistryError",
    "SourceValidationError",
]
