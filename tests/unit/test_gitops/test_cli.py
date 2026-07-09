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
