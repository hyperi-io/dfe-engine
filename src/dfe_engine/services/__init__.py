"""DFE Service configuration and state management.

Provides centralized configuration management and runtime state querying
for DFE Rust services (receiver, loader, archiver).

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- Backed by DirectoryConfigStore from hyperi-pylib
- In-memory cache with background polling refresh
- Optional git-aware writes (auto-commit, branch management, push)
- Rust services read the YAML files directly

Usage:
    from dfe_engine.services import ServiceConfigRegistry, ServiceStateClient
    from dfe_engine.services.models import ReceiverConfig, LoaderConfig, ArchiverConfig

    # Config management
    registry = ServiceConfigRegistry.get_instance(
        config_directory="/etc/dfe/configs",
    )
    config = registry.get_config("receiver", "production")
    registry.save_config("loader", LoaderConfig(...), instance="staging")

    # Runtime state
    client = ServiceStateClient("receiver", "http://receiver:8080")
    status = await client.status()
"""

from dfe_engine.services.models import (
    SERVICE_ARCHIVER,
    SERVICE_CONFIG_CLASSES,
    SERVICE_LOADER,
    SERVICE_RECEIVER,
    VALID_SERVICES,
    ArchiverConfig,
    LoaderConfig,
    ReceiverConfig,
)
from dfe_engine.services.registry import (
    ConfigNotFoundError,
    ServiceConfigError,
    ServiceConfigRegistry,
)
from dfe_engine.services.state import (
    HealthStatus,
    ServiceStateClient,
    ServiceStatus,
)
from dfe_engine.services.templates import generate_template
from dfe_engine.services.validators import ValidationResult, validate_config

__all__ = [
    # Registry
    "ConfigNotFoundError",
    "ServiceConfigError",
    "ServiceConfigRegistry",
    # State
    "HealthStatus",
    "ServiceStateClient",
    "ServiceStatus",
    # Models
    "ArchiverConfig",
    "LoaderConfig",
    "ReceiverConfig",
    # Templates & Validation
    "ValidationResult",
    "generate_template",
    "validate_config",
    # Constants
    "SERVICE_ARCHIVER",
    "SERVICE_CONFIG_CLASSES",
    "SERVICE_LOADER",
    "SERVICE_RECEIVER",
    "VALID_SERVICES",
]
