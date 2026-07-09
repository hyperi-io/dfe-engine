"""Helm values compiler for DFE services.

Merges deployment config + service config + source routing + KEDA wiring
into complete values.yaml files per service instance. Output is written
to a git monorepo that Argo CD watches.

Usage:
    from dfe_engine.helm import HelmValuesCompiler, EnvironmentConfig

    env = EnvironmentConfig.from_yaml("environments/production.yaml")
    compiler = HelmValuesCompiler(deploy_reg, svc_reg, src_reg, env)
    result = compiler.compile_all()
    compiler.write_all(result, output_dir)
"""

from dfe_engine.helm.argo_app import (
    generate_application,
    generate_applications,
    generate_appproject,
)
from dfe_engine.helm.argo_rbac import generate_appproject_roles, generate_rbac_csv
from dfe_engine.helm.compiler import HelmValuesCompiler
from dfe_engine.helm.environment import (
    ArgoEnvironment,
    ArgoSyncPolicy,
    ClickHouseEnvironment,
    EnvironmentConfig,
    KafkaEnvironment,
    OTelEnvironment,
)
from dfe_engine.helm.models import (
    CompilationResult,
    HelmDeployMeta,
    HelmImage,
    HelmKeda,
    HelmKedaTrigger,
    HelmServiceValues,
)
from dfe_engine.helm.operations import ImperativeOperations, OperationResult

__all__ = [
    "ArgoEnvironment",
    "ArgoSyncPolicy",
    "ClickHouseEnvironment",
    "CompilationResult",
    "EnvironmentConfig",
    "HelmDeployMeta",
    "HelmImage",
    "HelmKeda",
    "HelmKedaTrigger",
    "HelmServiceValues",
    "HelmValuesCompiler",
    "ImperativeOperations",
    "KafkaEnvironment",
    "OTelEnvironment",
    "OperationResult",
    "generate_application",
    "generate_applications",
    "generate_appproject",
    "generate_appproject_roles",
    "generate_rbac_csv",
]
