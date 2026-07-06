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
    HelmKeda,
    HelmKedaTrigger,
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


def test_publish_merge_preserves_operator_helm_edit(tmp_path) -> None:
    # FIX 7: a var set via the /helm path (helmvars resource 'receiver-prod-values'
    # -> values/receiver-prod-values.yaml) must SURVIVE the next publish, which
    # otherwise regenerates that file wholesale from the registry.
    from dfe_engine.gitcrud import GitCrud, default_registry
    from dfe_engine.gitops.repo import GitopsRepo

    gc = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    # simulate a prior publish landing the registry output, then an operator edit
    gc.put(
        "helmvars",
        "receiver-prod-values",
        {"replicaCount": 2, "image": {"repository": "ghcr.io/x/dfe-receiver", "tag": "2.2.0"}},
        actor="ci",
    )
    gc.set_key("helmvars", "receiver-prod-values", "replicaCount", 99, actor="operator")
    committed = (gc.repo_path / "values" / "receiver-prod-values.yaml").read_text()

    arts = collect_deploy_artifacts(
        _result(),  # registry base has replicaCount=2 for values/receiver-prod-values.yaml
        existing={"values/receiver-prod-values.yaml": committed},
    )
    merged = yaml_load_string(arts["values/receiver-prod-values.yaml"])
    assert merged["replicaCount"] == 99  # operator's edit WON, not reverted to 2
    # registry-managed keys still present
    assert merged["image"]["repository"] == "ghcr.io/x/dfe-receiver"
    assert merged["deploy"] == {"service": "dfe-receiver", "instance": "prod"}


def test_collect_without_existing_is_wholesale_regenerate() -> None:
    # default (no existing) keeps the pure regenerate behaviour - no merge.
    arts = collect_deploy_artifacts(_result())
    values = yaml_load_string(arts["values/receiver-prod-values.yaml"])
    assert values["replicaCount"] == 2


def _result_with_list() -> CompilationResult:
    # A values file carrying a NON-EMPTY list (keda.triggers) shared between the
    # registry base and a re-published committed file - the P1 list-duplication case.
    return CompilationResult(
        helm_values={
            "receiver-prod": HelmServiceValues(
                deploy=HelmDeployMeta(service="dfe-receiver", instance="prod"),
                image=HelmImage(repository="ghcr.io/x/dfe-receiver", tag="2.2.0"),
                keda=HelmKeda(
                    enabled=True,
                    triggers=[HelmKedaTrigger(type="metrics-api", metadata={"k": "v"})],
                ),
            )
        },
    )


def test_publish_over_own_output_is_idempotent_for_lists() -> None:
    # P1.1: publish must be idempotent. Re-publishing an unchanged registry over
    # its own committed output must be byte-identical - deep_merge's list-EXTEND
    # otherwise duplicates every list (keda.triggers, kafka.brokers, ...) on every
    # publish, growing unboundedly and never reaching 'repo already up to date'.
    first = collect_deploy_artifacts(_result_with_list())
    path = "values/receiver-prod-values.yaml"
    # feed the first output back in as the committed file (steady-state re-publish)
    second = collect_deploy_artifacts(_result_with_list(), existing={path: first[path]})
    assert second[path] == first[path]  # byte-identical: no list growth
    triggers = yaml_load_string(second[path])["keda"]["triggers"]
    assert len(triggers) == 1  # not doubled


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
