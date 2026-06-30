#  Project:      dfe-engine
#  File:         tests/governance/test_ch_rbac.py
#  Purpose:      Tests for per-group ClickHouse identity DDL (grants+quota+profile)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Per-group CH user DDL generation (the three controls) + gitops artifact."""

from __future__ import annotations

from dfe_engine.governance.ch_rbac import (
    GroupChBinding,
    build_group_sql,
    ddl_artifact,
)


def _binding():
    return GroupChBinding(
        group="soc-ro",
        grants=["SELECT ON dfe.*"],
        settings={"max_memory_usage": 10_000_000_000, "readonly": 1},
        quota={"queries": 1000, "result_rows": 1_000_000_000},
        quota_interval="1 hour",
    )


def test_default_names():
    b = GroupChBinding(group="soc-ro")
    assert b.user() == "dfe_grp_soc-ro"
    assert b.profile() == "dfe_grp_soc-ro_profile"
    assert b.quota_name() == "dfe_grp_soc-ro_quota"


def test_build_group_sql_has_all_three_controls():
    sql = build_group_sql(_binding(), "deadbeef")
    joined = "\n".join(sql)
    # identifiers are backtick-quoted (hyphenated group names must be valid CH names)
    assert "CREATE USER IF NOT EXISTS `dfe_grp_soc-ro`" in joined  # identity
    assert "GRANT SELECT ON dfe.* TO `dfe_grp_soc-ro`" in joined  # data scope
    assert "CREATE SETTINGS PROFILE IF NOT EXISTS `dfe_grp_soc-ro_profile`" in joined  # limits
    assert "max_memory_usage = 10000000000" in joined
    assert "ALTER USER `dfe_grp_soc-ro` SETTINGS PROFILE `dfe_grp_soc-ro_profile`" in joined
    assert "CREATE QUOTA IF NOT EXISTS `dfe_grp_soc-ro_quota`" in joined  # rate/volume
    assert "FOR INTERVAL 1 hour MAX" in joined


def test_ddl_artifact_is_gitops_path_and_sql():
    path, sql = ddl_artifact(_binding(), "deadbeef")
    assert path == "ddl/ch-rbac/soc-ro.sql"
    assert sql.startswith("-- DFE Governed Ops")
    assert "CREATE USER" in sql
    assert sql.rstrip().endswith(";")


def test_minimal_binding_just_creates_user():
    sql = build_group_sql(GroupChBinding(group="g"), "h")
    assert sql == ["CREATE USER IF NOT EXISTS `dfe_grp_g` IDENTIFIED WITH sha256_hash BY 'h'"]


def test_collect_deploy_artifacts_emits_ch_rbac_ddl():
    from dfe_engine.gitops.artifacts import collect_deploy_artifacts
    from dfe_engine.helm.models import CompilationResult

    result = CompilationResult(helm_values={})
    arts = collect_deploy_artifacts(result, ch_rbac=[(_binding(), "deadbeef")])
    assert "ddl/ch-rbac/soc-ro.sql" in arts
    assert "CREATE USER" in arts["ddl/ch-rbac/soc-ro.sql"]
