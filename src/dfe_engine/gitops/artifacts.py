#  Project:      dfe-engine
#  File:         gitops/artifacts.py
#  Purpose:      Map compiled artifacts to deploy-repo relative paths (pure)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Map compiled artifacts to deploy-repo relative paths.

Pure (no I/O): turns a :class:`CompilationResult` (+ optional rendered DDL) into
a ``{repo_relative_path: content}`` dict that :class:`GitopsRepo` commits. The
layout mirrors what the dfe-infra Argo apps expect to consume.
"""

from __future__ import annotations

from dfe_engine.helm.models import CompilationResult
from dfe_engine.yaml_utils import yaml_dump_string


def collect_deploy_artifacts(
    result: CompilationResult,
    *,
    environment: str,
    ddl: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return ``{repo_relative_path: content}`` for the deploy repo.

    Args:
        result: Compiled Helm/Argo artifacts from ``HelmValuesCompiler``.
        environment: Environment name (used in the AppProject filename).
        ddl: Optional ``{table_name: sql}`` from ``DDLFileWriter.generate_all()``.

    Returns:
        Mapping of repo-relative path to file content.
    """
    artifacts: dict[str, str] = {}

    artifacts[f"argocd/appproject-{environment}.yaml"] = yaml_dump_string(result.argo_appproject)
    for app in result.argo_applications:
        name = app["metadata"]["name"]
        artifacts[f"argocd/applications/{name}.yaml"] = yaml_dump_string(app)

    artifacts["argocd/rbac/argocd-rbac-policy.csv"] = result.argo_rbac_csv

    for key, values in result.helm_values.items():
        artifacts[f"values/{key}-values.yaml"] = yaml_dump_string(values.model_dump())

    for name, sql in (ddl or {}).items():
        artifacts[f"ddl/{name}.sql"] = sql

    return artifacts
