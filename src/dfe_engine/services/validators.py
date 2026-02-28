"""Cross-field validation for DFE service configurations.

Mirrors the Rust `validate()` methods to catch configuration errors
before they reach the services. Validation logic is dispatched via
the plugin system — each plugin carries its own validate_config callback.
"""

from __future__ import annotations

from pydantic import BaseModel

from dfe_engine.services.plugins import get_plugin, valid_services


class ValidationResult(BaseModel):
    """Result of configuration validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []


def validate_config(service: str, config_data: dict) -> ValidationResult:
    """Validate a service configuration.

    Performs both Pydantic model validation (types, constraints) and
    cross-field business logic validation (dispatched via plugin).

    Args:
        service: Service name (e.g. 'receiver', 'loader', 'archiver')
        config_data: Configuration dictionary

    Returns:
        ValidationResult with errors and warnings
    """
    services = valid_services()
    if service not in services:
        return ValidationResult(
            valid=False,
            errors=[f"Unknown service: {service}. Valid: {', '.join(sorted(services))}"],
        )

    plugin = get_plugin(service)
    config_cls = plugin.config_class
    errors: list[str] = []
    warnings: list[str] = []

    # Phase 1: Pydantic model validation
    try:
        config = config_cls.model_validate(config_data)
    except Exception as e:
        return ValidationResult(valid=False, errors=[f"Schema validation failed: {e}"])

    # Phase 2: Cross-field validation via plugin callback
    if plugin.validate_config is not None:
        plugin.validate_config(config, errors, warnings)

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)
