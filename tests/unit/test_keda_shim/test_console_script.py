#  Project:      dfe-engine
#  File:         tests/unit/test_keda_shim/test_console_script.py
#  Purpose:      dfe-keda-shim console entrypoint sets up scalo's logger first
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The shim's console entrypoint installs scalo's logger before anything else runs.

scalo's ``setup`` is replaced with a recorder and the settings load raises, so the
command ends straight after logger init and no sink or server is started.
"""

import pytest
import scalo._env_compat
from scalo.cli.error import LoggerError
from typer.testing import CliRunner

from dfe_engine.keda_shim import cli

runner = CliRunner()


class _StoppedAtSettings(Exception):
    """Raised in place of the settings load, so the command ends straight after logger init."""


@pytest.fixture
def setup_calls(monkeypatch) -> list[dict]:
    """Record each scalo logger ``setup`` call; the command stops at its settings load."""
    calls: list[dict] = []
    monkeypatch.setattr("scalo.logger.setup", lambda **kwargs: calls.append(kwargs))

    def _stop() -> None:
        raise _StoppedAtSettings

    monkeypatch.setattr(cli, "load_settings", _stop)
    # init_logger exports LOG_* and load_config sets scalo's global env prefix; undo both.
    for name in ("LOG_LEVEL", "LOG_FORMAT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setattr(scalo._env_compat, "_prefix_override", scalo._env_compat._prefix_override)
    return calls


def test_a_bare_invocation_sets_up_the_scalo_logger_once_before_settings(setup_calls):
    # No arguments: the deployed pod runs the console script bare.
    result = runner.invoke(cli.app, [])

    assert isinstance(result.exception, _StoppedAtSettings)
    assert len(setup_calls) == 1
    assert setup_calls[0]["service_name"] == "dfe-keda-shim"
    assert setup_calls[0]["otel_tracing"] is True


def test_log_level_and_format_env_reach_the_logger(setup_calls, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_FORMAT", "json")

    runner.invoke(cli.app, [])

    assert setup_calls[0]["level"] == "WARNING"
    assert setup_calls[0]["log_format"] == "json"


def test_a_logger_that_cannot_start_stops_the_shim(setup_calls, monkeypatch):
    def _broken(**_kwargs) -> None:
        raise OSError("sink unavailable")

    monkeypatch.setattr("scalo.logger.setup", _broken)

    result = runner.invoke(cli.app, [])

    assert isinstance(result.exception, LoggerError)
