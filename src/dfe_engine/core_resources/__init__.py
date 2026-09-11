"""Guards for API mutations on resources with ``resource_type: core`` in YAML.

Only the leaf re-exports here. ``policy`` reaches the schema and field-map
registries, so importing it from this package would put every consumer of
``yaml_resource_type`` behind that graph -- and the source models, which sit
under it, would close an import cycle. Import ``policy`` from its own module.
"""

from dfe_engine.core_resources.yaml_resource_type import (
    CORE_RESOURCE_MUTATION_MESSAGE,
    ResourceType,
    config_is_core,
    resource_type_from_config,
)

__all__ = [
    "CORE_RESOURCE_MUTATION_MESSAGE",
    "ResourceType",
    "config_is_core",
    "resource_type_from_config",
]
