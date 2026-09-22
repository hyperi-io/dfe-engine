#  Project:      dfe-engine
#  File:         src/dfe_engine/exchange/__init__.py
#  Purpose:      Import and export of meta schemas and source bundles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Moving a meta schema or a source between deployments.

``build_*`` reads the registries and produces a portable document; ``apply_*``
takes one back and writes it through the registry that already commits to git.
"""

from dfe_engine.exchange.models import (
    EXCHANGE_FORMAT,
    BundleSchema,
    MetaSchemaExport,
    MetaSchemaImportResult,
    ResourceReference,
    SourceBundle,
    SourceImportResult,
)
from dfe_engine.exchange.schemas import (
    ExchangeConflictError,
    ExchangeError,
    ExchangeUnresolvedError,
    apply_meta_schema_export,
    build_meta_schema_export,
)
from dfe_engine.exchange.sources import apply_source_bundle, build_source_bundle

__all__ = [
    "EXCHANGE_FORMAT",
    "BundleSchema",
    "ExchangeConflictError",
    "ExchangeError",
    "ExchangeUnresolvedError",
    "MetaSchemaExport",
    "MetaSchemaImportResult",
    "ResourceReference",
    "SourceBundle",
    "SourceImportResult",
    "apply_meta_schema_export",
    "apply_source_bundle",
    "build_meta_schema_export",
    "build_source_bundle",
]
