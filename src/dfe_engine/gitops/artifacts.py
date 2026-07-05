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

The values/*.yaml files are ALSO the target of the /helm var API (the helmvars
class writes ``values/<name>-values.yaml``), so a plain wholesale regenerate here
would silently revert an operator's Tier-1 edits on the next ``gitops publish``.
Pass ``existing`` (the committed file content) and each values file is merged
registry-base-UNDER-committed-overlay: an operator's var wins, the registry fills
in the rest. See collect_deploy_artifacts.
"""

from __future__ import annotations

from collections.abc import Mapping

from dfe_engine.helm.models import CompilationResult, HelmServiceValues
from dfe_engine.yaml_utils import deep_merge, yaml_dump_string, yaml_load_string


def collect_deploy_artifacts(
    result: CompilationResult,
    *,
    ddl: dict[str, str] | None = None,
    ch_rbac_ddl: list[str] | None = None,
    existing: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return ``{repo_relative_path: content}`` for the deploy repo.

    Args:
        result: Compiled overlay values from ``HelmValuesCompiler``.
        ddl: Optional ``{table_name: sql}`` from ``DDLFileWriter.generate_all()``.
        ch_rbac_ddl: Optional rendered CH-RBAC DDL statements (from
            ``governance.ch.ChRbacReconciler`` - tiers / service roles / per-org
            row policies / group users). Written to ``ddl/ch-rbac.sql`` so the
            shared migration runner can rebuild CH RBAC from git if the engine is
            absent (survivability).
        existing: Optional ``{repo_relative_path: committed_yaml_text}`` of the
            files already in the deploy repo. When a ``values/*.yaml`` file is
            already committed, the registry-derived base is merged UNDER the
            committed overlay (operator var edits win; registry fills the rest) so
            a publish PRESERVES /helm-applied edits instead of wholesale-reverting
            them. Trade-off: a registry change to a key an operator already
            overrode is shadowed by the committed value (preserving edits is the
            explicit operator decision); brand-new registry keys still land.

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
        path = f"values/{key}-values.yaml"
        prior = (existing or {}).get(path)
        if prior is not None:
            committed = yaml_load_string(prior) or {}
            if isinstance(committed, dict):
                # deep_merge mutates + lets the override (committed operator edits)
                # win over the registry base for any shared key.
                content = deep_merge(dict(content), committed)
        artifacts[path] = yaml_dump_string(content)

    for name, sql in (ddl or {}).items():
        artifacts[f"ddl/{name}.sql"] = sql

    if ch_rbac_ddl:
        artifacts["ddl/ch-rbac.sql"] = ";\n".join(ch_rbac_ddl) + ";\n"

    return artifacts
