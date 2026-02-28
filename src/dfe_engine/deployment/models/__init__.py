"""Pydantic deployment configuration models for DFE services."""

from dfe_engine.deployment.models.archiver import ArchiverDeploymentConfig
from dfe_engine.deployment.models.common import (
    BaseDeploymentConfig,
    HpaConfig,
    K8sServiceConfig,
    KedaConfig,
    KedaTriggerCpu,
    KedaTriggerKafka,
    PodConfig,
    ResourceQuantity,
    ResourceSpec,
    SecretRef,
    TShirtSize,
)
from dfe_engine.deployment.models.fetcher import FetcherDeploymentConfig
from dfe_engine.deployment.models.loader import LoaderDeploymentConfig
from dfe_engine.deployment.models.receiver import ReceiverDeploymentConfig
from dfe_engine.deployment.models.transform_vector import (
    TransformVectorDeploymentConfig,
)
from dfe_engine.deployment.models.transform_wasm import (
    TransformWasmDeploymentConfig,
)

# Service name constants (reused from services module)
DEPLOY_RECEIVER = "receiver"
DEPLOY_LOADER = "loader"
DEPLOY_ARCHIVER = "archiver"
DEPLOY_TRANSFORM_VECTOR = "transform-vector"
DEPLOY_TRANSFORM_WASM = "transform-wasm"
DEPLOY_FETCHER = "fetcher"

# Backward-compatible static sets (prefer plugins.valid_services() for dynamic lookup)
VALID_DEPLOY_SERVICES = {
    DEPLOY_RECEIVER, DEPLOY_LOADER, DEPLOY_ARCHIVER,
    DEPLOY_TRANSFORM_VECTOR, DEPLOY_TRANSFORM_WASM, DEPLOY_FETCHER,
}

# Backward-compatible config class mapping (prefer plugins.deployment_classes())
DEPLOY_CONFIG_CLASSES: dict[str, type] = {
    DEPLOY_RECEIVER: ReceiverDeploymentConfig,
    DEPLOY_LOADER: LoaderDeploymentConfig,
    DEPLOY_ARCHIVER: ArchiverDeploymentConfig,
    DEPLOY_TRANSFORM_VECTOR: TransformVectorDeploymentConfig,
    DEPLOY_TRANSFORM_WASM: TransformWasmDeploymentConfig,
    DEPLOY_FETCHER: FetcherDeploymentConfig,
}

__all__ = [
    # Base
    "BaseDeploymentConfig",
    # Constants
    "DEPLOY_ARCHIVER",
    "DEPLOY_CONFIG_CLASSES",
    "DEPLOY_FETCHER",
    "DEPLOY_LOADER",
    "DEPLOY_RECEIVER",
    "DEPLOY_TRANSFORM_VECTOR",
    "DEPLOY_TRANSFORM_WASM",
    "VALID_DEPLOY_SERVICES",
    # Common
    "HpaConfig",
    "K8sServiceConfig",
    "KedaConfig",
    "KedaTriggerCpu",
    "KedaTriggerKafka",
    "PodConfig",
    "ResourceQuantity",
    "ResourceSpec",
    "SecretRef",
    "TShirtSize",
    # Per-service
    "ArchiverDeploymentConfig",
    "FetcherDeploymentConfig",
    "LoaderDeploymentConfig",
    "ReceiverDeploymentConfig",
    "TransformVectorDeploymentConfig",
    "TransformWasmDeploymentConfig",
]
