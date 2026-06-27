#  Project:      dfe-engine
#  File:         gitops/artifacts.py
#  Purpose:      Map compiled overlay values to deploy-repo relative paths (pure)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Map compiled artifacts to deploy-repo relative paths.

Pure (no I/O): turns a :class:`CompilationResult` (+ optional rendered DDL) into
a ``{repo_relative_path: content}`` dict that :class:`GitopsRepo` commits.

The engine writes ONLY the OVERLAY into the deploy repo -- per-service-instance
Helm values (consumed by dfe-infra's ApplicationSets via Argo multi-source
``$values``) and DDL. It does NOT author Argo Application/AppProject/RBAC: that is
dfe-infra's deployment machinery (the appsets + the git-generator fan out per
values file). Values are dumped with ``exclude_none`` so omitted fields (e.g. a
KEDA ``triggers: None``) do not clobber the base chart's defaults on merge.
"""

from __future__ import annotations

from dfe_engine.helm.models import CompilationResult, HelmServiceValues
from dfe_engine.yaml_utils import yaml_dump_string


def collect_deploy_artifacts(
    result: CompilationResult,
    *,
    ddl: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return ``{repo_relative_path: content}`` for the deploy repo.

    Args:
        result: Compiled overlay values from ``HelmValuesCompiler``.
        ddl: Optional ``{table_name: sql}`` from ``DDLFileWriter.generate_all()``.

    Returns:
        Mapping of repo-relative path to file content (values/ + ddl/ only).
    """
    artifacts: dict[str, str] = {}

    for key, values in result.helm_values.items():
        if isinstance(values, HelmServiceValues):
            content = values.model_dump(mode="json", exclude_none=True)
        else:
            # External (Mode 2 / BYO chart) component: raw values dict, passthrough.
            content = values
        artifacts[f"values/{key}-values.yaml"] = yaml_dump_string(content)

    for name, sql in (ddl or {}).items():
        artifacts[f"ddl/{name}.sql"] = sql

    return artifacts
