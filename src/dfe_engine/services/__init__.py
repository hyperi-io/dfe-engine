"""DFE Service configuration management.

Provides centralized configuration management for DFE Rust services via a
plugin-based architecture.

Storage model:
- YAML directory is the Single Source of Truth (SSoT)
- Backed by DirectoryConfigStore from scalo
- In-memory cache with background polling refresh
- Optional git-aware writes (auto-commit, branch management, push)
- Rust services read the YAML files directly

Plugin system:
- Each service is a self-contained ServicePlugin
- Plugins are discovered via importlib.metadata entry_points
- External packages can register additional services

Usage:
    from dfe_engine.services import ServiceConfigRegistry
    from dfe_engine.services.plugins import get_plugin, valid_services
    from dfe_engine.services.models import ReceiverConfig, LoaderConfig, ArchiverConfig

    # Config management
    registry = ServiceConfigRegistry.get_instance(
        config_directory="/etc/dfe/configs",
    )
    config = registry.get_config("receiver", "production")
    registry.save_config("loader", LoaderConfig(...), instance="staging")

    # Plugin system
    all_services = valid_services()  # dynamic set from all registered plugins
    plugin = get_plugin("receiver")
    print(plugin.descriptor.image)
"""

from dfe_engine.services.descriptor import KafkaRole, ServiceDescriptor
from dfe_engine.services.models import (
    SERVICE_ARCHIVER,
    SERVICE_CONFIG_CLASSES,
    SERVICE_LOADER,
    SERVICE_RECEIVER,
    VALID_SERVICES,
    ArchiverConfig,
    BaseServiceConfig,
    LoaderConfig,
    ReceiverConfig,
)
from dfe_engine.services.plugin import ServicePlugin
from dfe_engine.services.registry import (
    ConfigNotFoundError,
    ServiceConfigError,
    ServiceConfigRegistry,
)
from dfe_engine.services.templates import generate_template
from dfe_engine.services.validators import ValidationResult, validate_config

__all__ = [
    # Plugin system
    "KafkaRole",
    "ServiceDescriptor",
    "ServicePlugin",
    # Base
    "BaseServiceConfig",
    # Registry
    "ConfigNotFoundError",
    "ServiceConfigError",
    "ServiceConfigRegistry",
    # Models
    "ArchiverConfig",
    "LoaderConfig",
    "ReceiverConfig",
    # Templates & Validation
    "ValidationResult",
    "generate_template",
    "validate_config",
    # Constants (backward compat)
    "SERVICE_ARCHIVER",
    "SERVICE_CONFIG_CLASSES",
    "SERVICE_LOADER",
    "SERVICE_RECEIVER",
    "VALID_SERVICES",
]
