"""DFE Source model -- the top-level data abstraction.

A Source represents a data stream entering the DFE platform. It contains
identity, schema, naming-standard views, and optional fetcher/transform
configuration.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- Backed by DirectoryConfigStore from scalo
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

from dfe_engine.source.expression import (
    ExpressionBuilder,
    ExpressionValidator,
    ExprValidationResult,
    list_directive_types,
)
from dfe_engine.source.models import (
    SchemaColumn,
    Source,
    SourceFetcher,
    SourceHeader,
    SourceMatch,
    SourceSchema,
    SourceTransform,
    SourceView,
)
from dfe_engine.source.registry import (
    SourceMatchConflictError,
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
    # Expression Validator
    "ExpressionBuilder",
    "ExpressionValidator",
    "ExprValidationResult",
    "list_directive_types",
    # Type Registry
    "InvalidAttributeError",
    "InvalidChOverrideError",
    "InvalidUseCaseError",
    "ResolvedType",
    "TypeRegistry",
    "TypeRegistryError",
    "UnknownPrimitiveError",
    # Models
    "SchemaColumn",
    "Source",
    "SourceFetcher",
    "SourceHeader",
    "SourceMatch",
    "SourceSchema",
    "SourceTransform",
    "SourceView",
    # Registry
    "SourceMatchConflictError",
    "SourceNotFoundError",
    "SourceRegistry",
    "SourceRegistryError",
    "SourceValidationError",
]
