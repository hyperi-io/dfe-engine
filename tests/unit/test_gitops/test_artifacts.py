"""Tests for collect_deploy_artifacts (pure mapping, no I/O).

The engine writes ONLY overlay values + DDL -- no Argo Application/AppProject/RBAC
(dfe-infra's appsets own deployment).
"""

from __future__ import annotations

from dfe_engine.gitops.artifacts import collect_deploy_artifacts
from dfe_engine.helm.models import (
    CompilationResult,
    HelmDeployMeta,
    HelmImage,
    HelmServiceValues,
)
from dfe_engine.yaml_utils import yaml_load_string


def _result() -> CompilationResult:
    return CompilationResult(
        helm_values={
            "receiver-prod": HelmServiceValues(
                deploy=HelmDeployMeta(service="dfe-receiver", instance="prod"),
                image=HelmImage(repository="ghcr.io/x/dfe-receiver", tag="2.2.0"),
                replicaCount=2,
            )
        },
    )


def test_collect_maps_only_values_and_ddl() -> None:
    arts = collect_deploy_artifacts(_result(), ddl={"events": "CREATE TABLE events (id UInt64);\n"})
    assert set(arts) == {
        "values/receiver-prod-values.yaml",
        "ddl/events.sql",
    }
    # No Argo authoring -- that is dfe-infra's job.
    assert not any(k.startswith("argocd/") for k in arts)


def test_collect_values_are_chart_shaped_and_carry_deploy_meta() -> None:
    arts = collect_deploy_artifacts(_result(), ddl=None)
    values = yaml_load_string(arts["values/receiver-prod-values.yaml"])
    assert values["replicaCount"] == 2
    assert values["image"]["repository"] == "ghcr.io/x/dfe-receiver"
    assert values["deploy"] == {"service": "dfe-receiver", "instance": "prod"}
    # keda.triggers omitted (None) so it cannot clobber the chart default trigger.
    assert "triggers" not in values["keda"]


def test_collect_without_ddl_omits_ddl_files() -> None:
    arts = collect_deploy_artifacts(_result(), ddl=None)
    assert not any(k.startswith("ddl/") for k in arts)
    assert "values/receiver-prod-values.yaml" in arts


def test_collect_ch_rbac_ddl_writes_one_survivability_file() -> None:
    arts = collect_deploy_artifacts(
        _result(),
        ch_rbac_ddl=[
            "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`",
            "GRANT SELECT ON dfe.* TO `dfe_analyst_tier_2_role`",
        ],
    )
    assert "ddl/ch-rbac.sql" in arts
    sql = arts["ddl/ch-rbac.sql"]
    assert "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`;" in sql
    assert sql.endswith(";\n")
