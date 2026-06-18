"""Guards for API mutations on resources with ``resource_type: core`` in YAML."""

from dfe_engine.core_resources.policy import MUTATION_METHODS, match_api_core_mutation
from dfe_engine.core_resources.yaml_resource_type import CORE_RESOURCE_MUTATION_MESSAGE

__all__ = [
    "CORE_RESOURCE_MUTATION_MESSAGE",
    "MUTATION_METHODS",
    "match_api_core_mutation",
]
