#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_cli.py
#  Purpose:      dfe-hunt-runner CLI wiring - command registration + param mapping
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Smoke-test the CLI WIRING (the runner/worker/daemon are tested elsewhere).

Two things this module guarantees without touching a live ClickHouse (and without
mocking one - the settings->params split keeps it honest):
  1. both process entrypoints (``run``, ``materialise``) are registered, so the
     console script exposes them - asserted via the Typer ``--help`` output;
  2. ``_ch_params`` maps the ClickHouse settings to the raw-client kwargs - a pure
     settings->dict mapping, so a field rename fails here instead of silently at
     connect time.

Executing ``run``/``materialise`` end-to-end needs a real CH; that is the Phase A
local-CH test, deliberately not attempted here.
"""

from __future__ import annotations

from typer.testing import CliRunner

from dfe_engine.hunt_runner import cli
from dfe_engine.settings import DFESettings

runner = CliRunner()


def test_help_lists_both_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "materialise" in result.output


def test_run_exposes_worker_id_option():
    # every pod MUST be able to carry a distinct lease-owner id; the derived
    # hostname-pid default covers the common case, the flag pins it explicitly
    result = runner.invoke(cli.app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--worker-id" in result.output


def test_ch_params_maps_settings_fields():
    # A field rename (username -> user, secure typo, ...) must fail HERE, not
    # silently at connect time - so pin the exact settings -> get_client kwargs.
    settings = DFESettings()
    settings.clickhouse.host = "ch.internal"
    settings.clickhouse.port = 8123
    settings.clickhouse.username = "dfe"
    settings.clickhouse.password = "secret"
    settings.clickhouse.secure = True
    settings.clickhouse.verify = False

    assert cli._ch_params(settings) == {
        "host": "ch.internal",
        "port": 8123,
        "username": "dfe",
        "password": "secret",
        "secure": True,
        "verify": False,
    }
