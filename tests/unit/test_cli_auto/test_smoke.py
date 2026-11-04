#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_smoke.py
#  Purpose:      The `dfe` command tree assembles from the live spec
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The command tree built from the LIVE engine spec, its mounted commands and the entry point."""

import click
import pytest
from typer.testing import CliRunner

from dfe_engine.cli.auto.app import build_cli
from dfe_engine.cli.auto.main import main


@pytest.fixture(scope="module")
def cli():
    return build_cli()


def test_tree_holds_generated_and_mounted_commands(cli):
    paths = set(cli.command_paths())
    assert {("orgs", "list"), ("orgs", "get"), ("auth", "accounts", "list")} <= paths
    assert {("auth", "list"), ("auth", "print-access-token"), ("login",), ("logout",)} <= paths
    assert {("config",), ("local",), ("schema",), ("kafka", "client-config")} <= paths
    assert any(path[:2] == ("kafka", "topics") for path in paths)


def test_every_visible_operation_has_a_command(cli):
    commanded = {
        cli.operation_at(*path).operation_id
        for path in cli.command_paths()
        if cli.operation_at(*path)
    }
    visible = {op.operation_id for op in cli.operations if not op.hidden}
    assert visible <= commanded


def test_hidden_operations_have_no_command(cli):
    assert not any(path[0] == "repository" for path in cli.command_paths())


def test_path_parameters_are_arguments(cli):
    args = ["orgs", "get", "--help"]
    # CI forces colour and its width wraps lines, so compare the plain text at a fixed width.
    usage = (
        CliRunner().invoke(cli.app(args), args, env={"COLUMNS": "200", "FORCE_COLOR": "1"}).stdout
    )
    assert "Example: dfe orgs get <name>" in click.unstyle(usage)


@pytest.mark.parametrize(
    "args", [["--help"], ["local", "--help"], ["schema", "--help"], ["config", "--help"]]
)
def test_help_builds(cli, args):
    result = CliRunner().invoke(cli.app(args), args)
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_local_help_lists_break_glass_commands(cli):
    result = CliRunner().invoke(cli.app(["local", "--help"]), ["local", "--help"])
    assert "break-glass" in result.output.lower()
    assert all(
        verb in result.output for verb in ("set", "get", "ls", "status", "revert", "ch-cloud")
    )


def test_console_entry_point_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dfe", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0
    assert "Usage:" in capsys.readouterr().out
