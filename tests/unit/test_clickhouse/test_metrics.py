#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_metrics.py
#  Purpose:      Per-query CH observability - opt-in, noop-safe, never raises
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CH metrics: opt-in via settings, degrades to log-only, and never raises."""

from __future__ import annotations

import pytest

import dfe_engine.settings as settings_module
from dfe_engine.clickhouse import metrics as chm
from dfe_engine.settings import ClickHouseSettings, DFESettings


@pytest.fixture(autouse=True)
def _reset_metrics_state():
    chm.reset_metrics_state()
    yield
    chm.reset_metrics_state()


def _settings(monkeypatch, *, enabled: bool) -> None:
    monkeypatch.setattr(
        settings_module,
        "_settings",
        DFESettings(clickhouse=ClickHouseSettings(metrics_enabled=enabled)),
    )


def test_disabled_by_default_is_log_only(monkeypatch):
    _settings(monkeypatch, enabled=False)
    # Must not raise, and no metric handles are built when metrics are off.
    chm.record_query(profile="query", operation="query", outcome="ok", duration_s=0.01, rows=5)
    assert chm._get_handles() is None


def test_enabled_builds_handles_and_records(monkeypatch):
    _settings(monkeypatch, enabled=True)
    # With a backend absent, scalo yields NoOp handles - either way, safe + no raise.
    chm.record_query(profile="query", operation="insert", outcome="error", duration_s=0.02)
    chm.record_query(profile="internal", operation="command", outcome="ok", duration_s=0.03)


def test_record_never_raises_even_with_broken_settings(monkeypatch):
    # A settings read that blows up must not surface into the query path.
    def _boom():
        raise RuntimeError("settings exploded")

    monkeypatch.setattr(settings_module, "get_settings", _boom)
    chm.record_query(profile="query", operation="query", outcome="ok", duration_s=0.01)


def test_reset_clears_lazily_built_state(monkeypatch):
    _settings(monkeypatch, enabled=False)
    chm._get_handles()
    assert chm._state == "disabled"
    chm.reset_metrics_state()
    assert chm._state == "uninit"
