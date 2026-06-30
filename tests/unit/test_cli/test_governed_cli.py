#  Project:      dfe-engine
#  File:         tests/unit/test_cli/test_governed_cli.py
#  Purpose:      Tests for the `dfe-api governed ...` CLI (wraps the gitcrud services)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Governed Ops CLI - exercises the real gitcrud over a tmp local repo."""

from __future__ import annotations

from typer.testing import CliRunner

from dfe_engine.cli.governed_ops import governed_app

runner = CliRunner()


def _enable_gitops(tmp_path, monkeypatch):
    monkeypatch.setenv("DFE_GITOPS_ENABLED", "true")
    monkeypatch.setenv("DFE_GITOPS_LOCAL_PATH", str(tmp_path / "deploy"))
    monkeypatch.setenv("DFE_GITOPS_PUSH", "false")
    monkeypatch.setenv("DFE_GITOPS_REPO_URL", "")


def test_set_then_list_and_get(tmp_path, monkeypatch):
    _enable_gitops(tmp_path, monkeypatch)
    r = runner.invoke(governed_app, ["helm", "set", "receiver-default", "keda.maxReplicas", "10"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(governed_app, ["helm", "list"])
    assert r.exit_code == 0
    assert "receiver-default" in r.output

    r = runner.invoke(governed_app, ["helm", "get", "receiver-default"])
    assert "keda.maxReplicas" in r.output
    assert "10" in r.output


def test_disabled_gitops_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("DFE_GITOPS_ENABLED", "false")
    r = runner.invoke(governed_app, ["helm", "list"])
    assert r.exit_code == 1
    assert "not enabled" in r.output
