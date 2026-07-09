#  Project:      dfe-engine
#  File:         ai/__init__.py
#  Purpose:      AI assist touch-points for authoring (framework + stubs)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""AI assist touch-points for rule/transform/schema authoring.

The interface defines the contracts; real models register via AIModuleRegistry.
Default stub implementations make the four touch-points functional until migrated -
see docs/superpowers/plans/2026-07-01-rules-hunts-versioning.md.
"""

from .interface import (
    AIModuleInterface,
    AIModuleRegistry,
    AIModuleResult,
    AIModuleStatus,
    AIModuleType,
    LogParser,
    QueryGenerator,
    QueryOptimiser,
    SchemaOptimiser,
)
from .sampling import discover_json_keys, sample_json_rows
from .stubs import (
    StubLogParser,
    StubQueryGenerator,
    StubQueryOptimiser,
    StubSchemaOptimiser,
    default_ai_registry,
)

__all__ = [
    "AIModuleInterface",
    "AIModuleRegistry",
    "AIModuleResult",
    "AIModuleStatus",
    "AIModuleType",
    "LogParser",
    "QueryGenerator",
    "QueryOptimiser",
    "SchemaOptimiser",
    "StubLogParser",
    "StubQueryGenerator",
    "StubQueryOptimiser",
    "StubSchemaOptimiser",
    "default_ai_registry",
    "discover_json_keys",
    "sample_json_rows",
]
