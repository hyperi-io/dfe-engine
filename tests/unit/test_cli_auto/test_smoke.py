#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_smoke.py
#  Purpose:      Generation-pipeline smoke: make_root / --help / entry point
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Regression guard that the whole generation pipeline assembles from the LIVE spec.

``make_root()`` loads the real engine OpenAPI document (``create_app().openapi()``)
and builds the full command tree + built-ins + the ``local`` break-glass group. A
crash anywhere in spec parsing / tree building / mounting would surface here. These
also cover the console-script ``main`` entry point (``dfe``) which the unit tests
otherwise never execute.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from dfe_engine.cli.auto.app import make_root
from dfe_engine.cli.auto.main import main


@pytest.fixture(scope="module")
def root():
    return make_root()


def test_make_root_assembles_generated_and_builtins():
    built = make_root()
    assert built.name == "dfe"
    # Generated resource group + built-ins + break-glass group all mounted.
    assert "orgs" in built.commands
    assert "login" in built.commands
    assert "config" in built.commands
    assert "local" in built.commands


def test_dfe_help_builds(root):
    result = CliRunner().invoke(root, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "orgs" in result.output
    assert "local" in result.output


def test_dfe_local_help_lists_break_glass_commands(root):
    result = CliRunner().invoke(root, ["local", "--help"])
    assert result.exit_code == 0, result.output
    assert "break-glass" in result.output.lower()
    # The core read/write break-glass verbs are present.
    for verb in ("set", "get", "ls", "status", "revert"):
        assert verb in result.output


def test_console_entry_point_help(monkeypatch, capsys):
    # `dfe --help` through the real console-script entry (main -> make_root -> run).
    monkeypatch.setattr("sys.argv", ["dfe", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0
    assert "Usage:" in capsys.readouterr().out
