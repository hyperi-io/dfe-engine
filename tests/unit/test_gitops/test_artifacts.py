"""Tests for collect_deploy_artifacts (pure mapping, no I/O)."""

from __future__ import annotations

from dfe_engine.gitops.artifacts import collect_deploy_artifacts
from dfe_engine.helm.models import CompilationResult, HelmServiceValues
from dfe_engine.yaml_utils import yaml_load_string


def _result() -> CompilationResult:
    return CompilationResult(
        helm_values={"receiver-prod": HelmServiceValues(image="ghcr.io/x/receiver")},
        argo_rbac_csv="p, role:dfe-admin, applications, *, */*, allow\n",
        argo_applications=[
            {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Application",
                "metadata": {"name": "dfe-engine"},
            }
        ],
        argo_appproject={
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "AppProject",
            "metadata": {"name": "dfe"},
        },
    )


def test_collect_maps_all_artifact_paths() -> None:
    arts = collect_deploy_artifacts(
        _result(), environment="local", ddl={"events": "CREATE TABLE events (id UInt64);\n"}
    )
    assert set(arts) == {
        "argocd/appproject-local.yaml",
        "argocd/applications/dfe-engine.yaml",
        "argocd/rbac/argocd-rbac-policy.csv",
        "values/receiver-prod-values.yaml",
        "ddl/events.sql",
    }


def test_collect_yaml_roundtrips_and_csv_verbatim() -> None:
    arts = collect_deploy_artifacts(_result(), environment="local", ddl=None)
    appproj = yaml_load_string(arts["argocd/appproject-local.yaml"])
    assert appproj["kind"] == "AppProject"
    assert appproj["metadata"]["name"] == "dfe"
    values = yaml_load_string(arts["values/receiver-prod-values.yaml"])
    assert values["image"] == "ghcr.io/x/receiver"
    assert arts["argocd/rbac/argocd-rbac-policy.csv"].startswith("p, role:dfe-admin")


def test_collect_without_ddl_omits_ddl_files() -> None:
    arts = collect_deploy_artifacts(_result(), environment="prod", ddl=None)
    assert not any(k.startswith("ddl/") for k in arts)
    assert "argocd/appproject-prod.yaml" in arts
