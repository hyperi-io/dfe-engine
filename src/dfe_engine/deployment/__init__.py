"""DFE Deployment configuration and sizing management.

Provides K8s deployment configuration management for DFE Rust services
(receiver, loader, archiver) with unified t-shirt sizing.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- Backed by DirectoryConfigStore from hyperi-pylib
- Deployment configs are separate from service configs (different lifecycle)
- T-shirt sizing bridges deployment and service config (buffer/memory scaling)

Usage:
    from dfe_engine.deployment import DeploymentConfigRegistry
    from dfe_engine.deployment.models import ReceiverDeploymentConfig, TShirtSize
    from dfe_engine.deployment.sizing import apply_sizing

    # Config management
    registry = DeploymentConfigRegistry.get_instance(
        config_directory="/etc/dfe/deploy",
    )
    config = registry.get_config("receiver", "production")

    # Apply sizing (returns service config overrides for the caller)
    svc_overrides = registry.apply_size("loader", "production", "large")

    # Export Helm values
    registry.export_helm_values("loader", "production", Path("values.yaml"))
"""

from dfe_engine.deployment.models import (
    DEPLOY_ARCHIVER,
    DEPLOY_CONFIG_CLASSES,
    DEPLOY_LOADER,
    DEPLOY_RECEIVER,
    VALID_DEPLOY_SERVICES,
    ArchiverDeploymentConfig,
    LoaderDeploymentConfig,
    ReceiverDeploymentConfig,
    TShirtSize,
)
from dfe_engine.deployment.registry import (
    DeploymentConfigError,
    DeploymentConfigNotFoundError,
    DeploymentConfigRegistry,
)
from dfe_engine.deployment.sizing import apply_sizing, get_resources, get_service_overrides
from dfe_engine.deployment.templates import generate_template
from dfe_engine.deployment.validators import ValidationResult, validate_deployment_config

__all__ = [
    # Registry
    "DeploymentConfigError",
    "DeploymentConfigNotFoundError",
    "DeploymentConfigRegistry",
    # Models
    "ArchiverDeploymentConfig",
    "LoaderDeploymentConfig",
    "ReceiverDeploymentConfig",
    "TShirtSize",
    # Sizing
    "apply_sizing",
    "get_resources",
    "get_service_overrides",
    # Templates & Validation
    "ValidationResult",
    "generate_template",
    "validate_deployment_config",
    # Constants
    "DEPLOY_ARCHIVER",
    "DEPLOY_CONFIG_CLASSES",
    "DEPLOY_LOADER",
    "DEPLOY_RECEIVER",
    "VALID_DEPLOY_SERVICES",
]
