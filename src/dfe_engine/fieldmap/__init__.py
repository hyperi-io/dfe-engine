"""DFE Field Mapping Layer — standard-to-DFE column mappings.

Supports Sigma, ECS, CIM, and custom standards. Each standard has
a default field map plus optional source-specific overrides.

Storage model:
    - YAML directory is the Single Source of Truth (SSoT)
    - Backed by DirectoryConfigStore from hyperi-pylib
    - Two-tier resolution: default + source-specific override

Usage:
    from dfe_engine.fieldmap import FieldMap, FieldMapRegistry
    from dfe_engine.fieldmap import resolve_field_map

    registry = FieldMapRegistry(field_maps_directory="/etc/dfe/field-maps")
    default = registry.get_map("sigma")
    specific = registry.get_map("sigma", "windows_audit")
    merged = resolve_field_map(default, specific)
"""

from dfe_engine.fieldmap.models import (
    DEFAULT_MAP_NAME,
    KNOWN_STANDARDS,
    FieldMap,
)
from dfe_engine.fieldmap.registry import (
    FieldMapError,
    FieldMapNotFoundError,
    FieldMapRegistry,
    FieldMapValidationError,
)
from dfe_engine.fieldmap.resolver import (
    resolve_field,
    resolve_field_map,
)

__all__ = [
    # Models
    "DEFAULT_MAP_NAME",
    "FieldMap",
    "KNOWN_STANDARDS",
    # Registry
    "FieldMapError",
    "FieldMapNotFoundError",
    "FieldMapRegistry",
    "FieldMapValidationError",
    # Resolver
    "resolve_field",
    "resolve_field_map",
]
