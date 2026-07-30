"""Cross-field validation for DFE deployment configurations.

Validates K8s resource specs, KEDA/HPA consistency, and sizing rules.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from dfe_engine.services.plugins import deployment_classes, valid_services


class ValidationResult(BaseModel):
    """Result of deployment configuration validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []


def validate_deployment_config(service: str, config_data: dict) -> ValidationResult:
    """Validate a deployment configuration.

    Performs Pydantic model validation followed by cross-field business logic.

    Args:
        service: Service name
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

    deploy_cls = deployment_classes().get(service)
    if deploy_cls is None:
        return ValidationResult(
            valid=False,
            errors=[f"No deployment config class registered for: {service}"],
        )

    errors: list[str] = []
    warnings: list[str] = []

    # Phase 1: Pydantic model validation
    try:
        config = deploy_cls.model_validate(config_data)
    except Exception as e:
        return ValidationResult(valid=False, errors=[f"Schema validation failed: {e}"])

    # Phase 2: Cross-field validation
    _validate_sizing(config, errors, warnings)
    _validate_autoscaling(config, errors, warnings)
    _validate_resources(config, errors, warnings)

    return ValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)


def _validate_sizing(config, errors: list[str], warnings: list[str]) -> None:
    """Validate sizing consistency."""
    if config.size.value == "custom" and config.resources is None:
        errors.append("size is 'custom' but no explicit resources provided")

    if config.size.value != "custom" and config.resources is not None:
        warnings.append(
            "Both size and explicit resources set — explicit resources will be ignored "
            "unless size is 'custom'"
        )


def _validate_autoscaling(config, errors: list[str], warnings: list[str]) -> None:
    """Validate KEDA and HPA consistency."""
    if config.keda.enabled and config.hpa.enabled:
        errors.append("KEDA and HPA cannot both be enabled")

    if config.keda.enabled:
        if config.keda.min_replicas > config.keda.max_replicas:
            errors.append(
                f"KEDA min_replicas ({config.keda.min_replicas}) > "
                f"max_replicas ({config.keda.max_replicas})"
            )
        if config.keda.kafka_trigger is None and config.keda.cpu_trigger is None:
            errors.append("KEDA enabled but no triggers configured")

    if config.hpa.enabled:
        if config.hpa.min_replicas > config.hpa.max_replicas:
            errors.append(
                f"HPA min_replicas ({config.hpa.min_replicas}) > "
                f"max_replicas ({config.hpa.max_replicas})"
            )


# K8s quantity suffixes, longest-first so "Ki" is matched before "K". Both the
# binary and the decimal SI family are valid in a manifest, so both have to parse
# here: a suffix this map misses is a quantity the requests-vs-limits comparison
# below cannot make.
_QUANTITY_SUFFIXES: dict[str, float] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def _parse_k8s_quantity(value: str) -> float:
    """Parse a K8s resource quantity to a comparable float.

    Raises:
        ValueError: If the quantity cannot be parsed. Callers must treat that
            as invalid, never as "no opinion" -- see _validate_resources.
    """
    if m := re.match(r"^(\d+)m$", value):
        return int(m.group(1)) / 1000.0

    for suffix, multiplier in _QUANTITY_SUFFIXES.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier

    return float(value)


def _validate_resources(config, errors: list[str], warnings: list[str]) -> None:
    """Validate resource requests <= limits."""
    if config.resources is None:
        return

    # An unparseable quantity is an ERROR, not a warning: warnings leave
    # valid=True, and a quantity that does not parse is one the comparison below
    # cannot make, so a warning here reports a config as valid that K8s rejects
    # at apply time.
    res = config.resources
    try:
        req_cpu = _parse_k8s_quantity(res.requests.cpu)
        lim_cpu = _parse_k8s_quantity(res.limits.cpu)
        if req_cpu > lim_cpu:
            errors.append(f"CPU requests ({res.requests.cpu}) > limits ({res.limits.cpu})")
    except ValueError:
        errors.append(
            f"Could not parse CPU quantities: requests={res.requests.cpu}, limits={res.limits.cpu}"
        )

    try:
        req_mem = _parse_k8s_quantity(res.requests.memory)
        lim_mem = _parse_k8s_quantity(res.limits.memory)
        if req_mem > lim_mem:
            errors.append(f"Memory requests ({res.requests.memory}) > limits ({res.limits.memory})")
    except ValueError:
        errors.append(
            f"Could not parse memory quantities: requests={res.requests.memory}, "
            f"limits={res.limits.memory}"
        )
