"""CLI tests for `dfe-api gitops` (real settings, no mocks)."""

from __future__ import annotations

from typer.testing import CliRunner

from dfe_engine.cli.gitops import gitops_app

runner = CliRunner()


def test_publish_disabled_short_circuits(monkeypatch) -> None:
    monkeypatch.setenv("DFE_GITOPS_ENABLED", "false")
    result = runner.invoke(gitops_app, ["publish"])
    assert result.exit_code == 0
    assert "disabled" in result.stdout.lower()


def test_publish_enabled_without_local_path_errors(monkeypatch) -> None:
    monkeypatch.setenv("DFE_GITOPS_ENABLED", "true")
    monkeypatch.delenv("DFE_GITOPS_LOCAL_PATH", raising=False)
    result = runner.invoke(gitops_app, ["publish"])
    assert result.exit_code == 1


def test_status_renders(monkeypatch) -> None:
    monkeypatch.setenv("DFE_GITOPS_BRANCH", "deploy")
    result = runner.invoke(gitops_app, ["status"])
    assert result.exit_code == 0
    assert "deploy" in result.stdout


def test_render_ch_rbac_ddl_has_tier_and_service_role_ddl() -> None:
    """5c.3: the survivability DDL renders the config-only tier + service-role
    objects, and skips the discovery/secret-dependent row policies + users."""
    from dfe_engine.cli.gitops import _render_ch_rbac_ddl

    joined = "\n".join(_render_ch_rbac_ddl())
    assert "CREATE ROLE IF NOT EXISTS `dfe_analyst_tier_2_role`" in joined  # a quota tier
    assert "CREATE ROLE IF NOT EXISTS `dfe_loader_role`" in joined  # a service role
    # discovery-dependent (row policies) + secret-dependent (minted users) skipped
    assert "ROW POLICY" not in joined
    assert "CREATE USER" not in joined


def test_assemble_artifacts_includes_ch_rbac_ddl_and_hyperdx_connections(tmp_path) -> None:
    """A publish's artifact set carries ddl/ch-rbac.sql AND the per-ORG HyperDX
    DEFAULT_CONNECTIONS JSON: every org connection uses the shared dfe_tenant_reader
    (secret ch/fixed/dfe_tenant_reader) and carries its DFE_current_tenant_id setting.
    Real registries + real file-backed secrets, no mocks."""
    import json

    from dfe_engine.cli.gitops import _assemble_artifacts
    from dfe_engine.helm.models import CompilationResult
    from dfe_engine.hyperdx.client import HYPERDX_CONNECTIONS_PATH
    from dfe_engine.orgs.registry import OrgRegistry
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import AuthSettings, DFESettings, SecretsSettings

    config_dir = tmp_path / "config"
    OrgRegistry(config_dir / "orgs").create("acme", org_ids=["acme", "acme-sub"])

    secrets_dir = tmp_path / "secrets"
    build_secrets(SecretsSettings(provider="file", path=str(secrets_dir))).put(
        "ch/fixed/dfe_tenant_reader", "reader-pw"
    )

    settings = DFESettings(
        config_dir=str(config_dir),
        secrets=SecretsSettings(provider="file", path=str(secrets_dir)),
        auth=AuthSettings(auth_dir=str(config_dir / "auth")),
    )

    artifacts = _assemble_artifacts(settings, CompilationResult(), {})

    # survivability DDL
    assert "ddl/ch-rbac.sql" in artifacts
    sql = artifacts["ddl/ch-rbac.sql"]
    assert "`dfe_analyst_tier_2_role`" in sql
    assert "`dfe_loader_role`" in sql

    # per-org HyperDX connections: shared reader + baked-in tenant setting
    assert HYPERDX_CONNECTIONS_PATH in artifacts
    conns = json.loads(artifacts[HYPERDX_CONNECTIONS_PATH])
    assert len(conns) == 1
    assert conns[0]["name"] == "acme"
    assert conns[0]["user"] == "dfe_tenant_reader"
    assert conns[0]["password"] == "reader-pw"
    assert conns[0]["clickhouseSettings"] == {"DFE_current_tenant_id": "acme,acme-sub"}
