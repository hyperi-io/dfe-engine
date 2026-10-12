#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_schema_cli.py
#  Purpose:      `dfe schema apply` runs the phase whether or not its metrics could be built
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``dfe schema apply`` builds its own metrics once, ahead of the phase it runs.

A fault building them must not stop the apply, so each test drives the command through
the real phase and reads the state it reports. ClickHouse is the one thing absent: the
bootstrap switched off, or the connection refused.
"""

import json

import pytest
from click.testing import CliRunner
from scalo.logger import logger

from dfe_engine.cli.auto.schema import schema_group
from dfe_engine.schema import phase
from dfe_engine.schema.manifest_applier import ManifestApplyError


@pytest.fixture(autouse=True)
def _cli_environment(monkeypatch):
    """Keep a developer's .env out, pin the metrics backend, and clear the phase state after."""
    # The Prometheus backend keeps the manager off the process-wide OTel provider.
    monkeypatch.setenv("METRICS_BACKEND", "prometheus")
    monkeypatch.setattr("dfe_engine.env_files.load_env_files", lambda: None)
    yield
    phase._set_state(phase.SchemaBootstrapState())


def _apply():
    result = CliRunner().invoke(schema_group, ["apply", "--json"])
    return result, json.loads(result.stdout)


def _refuse_connection(monkeypatch) -> None:
    def refused(*_args, **_kwargs):
        raise ManifestApplyError("ClickHouse did not answer")

    monkeypatch.setattr(phase, "_connect", refused)


def test_apply_with_the_bootstrap_off_reports_unknown_and_exits_clean(monkeypatch):
    monkeypatch.setenv("DFE_CLICKHOUSE_BOOTSTRAP_TABLES", "false")

    result, state = _apply()

    assert result.exit_code == 0, result.output
    assert state["state"] == "unknown"


def test_apply_reaches_clickhouse_with_its_metrics_built(monkeypatch):
    """The phase gets as far as connecting, and the refusal is its own failed state."""
    monkeypatch.setenv("DFE_CLICKHOUSE_BOOTSTRAP_TABLES", "true")
    _refuse_connection(monkeypatch)

    result, state = _apply()

    assert result.exit_code == 1, result.output
    assert state["state"] == "failed"
    assert "manifest apply" in state["error"]


def test_apply_still_runs_when_its_metrics_cannot_be_built(monkeypatch):
    """A fault in the metrics library is logged and the phase carries on without them."""
    monkeypatch.setenv("DFE_CLICKHOUSE_BOOTSTRAP_TABLES", "true")
    _refuse_connection(monkeypatch)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("meter provider refused")

    monkeypatch.setattr("scalo.metrics.create_metrics", unavailable)
    warnings: list[str] = []
    sink = logger.add(lambda message: warnings.append(message.record["message"]), level="WARNING")
    try:
        result, state = _apply()
    finally:
        logger.remove(sink)

    assert result.exit_code == 1, result.output
    assert state["state"] == "failed"
    assert "manifest apply" in state["error"]
    assert "schema metrics unavailable; the pass runs without them" in warnings
