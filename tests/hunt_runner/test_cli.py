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
  3. ``run`` rechecks the lookup databases after each reload, before it fires again,
     driven past its connect with fakes.

Executing ``run``/``materialise`` end-to-end needs a real CH; that is the Phase A
local-CH test, deliberately not attempted here.

The logger tests replace scalo's ``setup`` with a recorder and stop the command at
its settings load, so they prove the init order without installing a sink.
"""

import pytest
import scalo._env_compat
from scalo.cli.error import LoggerError
from typer.testing import CliRunner

from dfe_engine.api import _DfeEngineApp
from dfe_engine.clickhouse.tls import ClickHouseCaCertUnreadable
from dfe_engine.hunt_runner import cli
from dfe_engine.hunts.lookups import LookupDatabaseError
from dfe_engine.settings import DFESettings, HuntsSettings

runner = CliRunner()


class _StoppedAtSettings(Exception):
    """Raised in place of the settings load, so a command ends straight after logger init."""


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


@pytest.mark.parametrize(("command", "tracing"), [("materialise", False), ("run", True)])
def test_each_command_sets_up_the_scalo_logger_once_before_settings(setup_calls, command, tracing):
    result = runner.invoke(cli.app, [command])

    assert isinstance(result.exception, _StoppedAtSettings)
    assert len(setup_calls) == 1
    assert setup_calls[0]["service_name"] == "dfe-hunt-runner"
    assert setup_calls[0]["otel_tracing"] is tracing


def test_log_level_and_format_env_reach_the_logger(setup_calls, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("LOG_FORMAT", "json")

    runner.invoke(cli.app, ["materialise"])

    assert setup_calls[0]["level"] == "DEBUG"
    assert setup_calls[0]["log_format"] == "json"


def test_the_cascade_loads_under_the_daemon_prefix(setup_calls, monkeypatch):
    monkeypatch.setattr(scalo._env_compat, "_prefix_override", None)
    monkeypatch.delenv("ENV_PREFIX", raising=False)

    runner.invoke(cli.app, ["materialise"])

    assert scalo._env_compat.env_prefix() == _DfeEngineApp()._make_app().env_prefix


def test_a_logger_that_cannot_start_stops_the_command(setup_calls, monkeypatch):
    def _broken(**_kwargs) -> None:
        raise OSError("sink unavailable")

    monkeypatch.setattr("scalo.logger.setup", _broken)

    result = runner.invoke(cli.app, ["materialise"])

    assert isinstance(result.exception, LoggerError)


def test_help_lists_both_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "materialise" in result.output


def test_ch_params_maps_settings_fields():
    # A field rename (username -> user, secure typo, ...) must fail HERE, not
    # silently at connect time - so pin the exact settings -> get_client kwargs.
    #
    # env="test" because a bare DFESettings() defaults to the production posture,
    # which refuses the dev jwt placeholder. This test is about ClickHouse field
    # mapping, so it takes a dev posture rather than inventing a secret.
    settings = DFESettings(env="test")
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


def test_ch_params_carries_a_readable_ca_cert(tmp_path):
    ca = tmp_path / "internal-ca.pem"
    ca.write_text("cert")
    settings = DFESettings(env="test")
    settings.clickhouse.secure = True
    settings.clickhouse.ca_cert = str(ca)

    assert cli._ch_params(settings)["ca_cert"] == str(ca)


def test_ch_params_refuses_an_unreadable_ca_cert(tmp_path):
    missing = tmp_path / "does-not-exist.pem"
    settings = DFESettings(env="test")
    settings.clickhouse.secure = True
    settings.clickhouse.ca_cert = str(missing)

    with pytest.raises(ClickHouseCaCertUnreadable, match="DFE_CLICKHOUSE_CA_CERT"):
        cli._ch_params(settings)


def test_ch_params_omits_tls_kwargs_when_insecure():
    settings = DFESettings(env="test")
    settings.clickhouse.secure = False

    assert cli._ch_params(settings) == {
        "host": settings.clickhouse.host,
        "port": settings.clickhouse.port,
        "username": settings.clickhouse.username,
        "password": settings.clickhouse.password,
    }


def test_spec_sources_carry_the_configured_detection_cap():
    settings = DFESettings(env="test")
    settings.hunts.rules_dir = "/rules"
    settings.hunts.max_detections_per_run = 250

    assert cli._spec_sources(settings, "tenant_a") == {
        "rules_dir": "/rules",
        "default_target": "tenant_a.detection",
        "default_database": "tenant_a",
        "max_detections": 250,
        "lookup_databases": (),
    }


def test_spec_sources_carry_the_configured_lookup_databases():
    settings = DFESettings(env="test", hunts=HuntsSettings(lookup_databases=["threat", "geo"]))

    assert cli._spec_sources(settings, "dfe")["lookup_databases"] == ("threat", "geo")


def test_a_cap_above_its_ceiling_is_cut_to_the_ceiling():
    settings = DFESettings(env="test")
    settings.hunts.max_detections_per_run = 50_000
    settings.hunts.max_detections_per_run_ceiling = 10_000

    assert cli._spec_sources(settings, "dfe")["max_detections"] == 10_000


class _Ran(Exception):
    """Ends the run command's loop at the fake runner's fourth tick."""


class _Metrics:
    def clickhouse_unavailable(self, *, stage: str, reason: str) -> None:
        pass


@pytest.fixture
def run_events(monkeypatch) -> list[str]:
    """Wire ``run`` to fakes past its connect, recording each runner tick in order."""
    events: list[str] = []

    class _Runner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def tick(self, _now: int) -> int:
            events.append("tick")
            if events.count("tick") == 4:
                raise _Ran
            return 0

    settings = DFESettings(env="test", hunts=HuntsSettings(lookup_databases=["intel"]))
    monkeypatch.setattr(cli, "init_scalo_logger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli.runner_metrics, "create", _Metrics)
    monkeypatch.setattr(cli, "connect_when_available", lambda *_args, **_kwargs: ("ch", "coord"))
    monkeypatch.setattr(cli, "HuntWorker", lambda *_args, **_kwargs: "worker")
    monkeypatch.setattr(cli, "load_specs", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(cli, "HuntRunner", _Runner)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)
    return events


def test_a_lookup_table_turning_unsafe_stops_the_runner_at_its_next_reload(run_events, monkeypatch):
    def _refuse(client, databases) -> None:
        run_events.append(f"check {client} {','.join(databases)}")
        raise LookupDatabaseError("intel.sightings has an _org_id column")

    monkeypatch.setattr(cli, "refuse_tenant_lookups", _refuse)

    result = runner.invoke(cli.app, ["run", "--poll", "0", "--reload-every", "1"])

    assert isinstance(result.exception, LookupDatabaseError)
    assert run_events == ["tick", "check ch intel"]


def test_a_reload_check_clickhouse_cannot_answer_fires_nothing_until_it_passes(
    run_events, monkeypatch
):
    answers: list[Exception | None] = [ConnectionError("refused"), None, None, None]

    def _refuse(_client, _databases) -> None:
        answer = answers.pop(0)
        run_events.append("check" if answer is None else "check down")
        if answer is not None:
            raise answer

    monkeypatch.setattr(cli, "refuse_tenant_lookups", _refuse)

    result = runner.invoke(cli.app, ["run", "--poll", "0", "--reload-every", "1"])

    assert isinstance(result.exception, _Ran)
    assert run_events == ["tick", "check down", "check", "tick", "check", "tick", "check", "tick"]
