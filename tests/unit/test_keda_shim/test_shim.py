#  Project:      dfe-engine
#  File:         tests/unit/test_keda_shim/test_shim.py
#  Purpose:      QueryShim fail-safe + clamp + injection-guard unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unit tests for the KEDA shim's query runner and (critically) its fail-safe.

No live ClickHouse: a fake client is injected, so these run with the rest of the
unit suite. The fail-safe tests are the important ones - a metric outage must hold
last-good / cold value, never scale up.
"""

from unittest.mock import patch

import pytest

from dfe_engine.keda_shim.shim import QueryShim, _ch_client
from dfe_engine.settings import DFESettings


class _FakeResult:
    def __init__(self, rows: list[list]) -> None:
        self.result_rows = rows


class _FakeClient:
    """Records the SQL/params it was asked to run; returns canned rows or raises."""

    def __init__(self, rows: list[list] | None = None, error: Exception | None = None) -> None:
        self.rows = rows if rows is not None else [[0]]
        self.error = error
        self.calls: list[tuple] = []

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters, settings))
        if self.error is not None:
            raise self.error
        return _FakeResult(self.rows)


def _shim(client: _FakeClient) -> QueryShim:
    return QueryShim(DFESettings(env="test"), client_factory=lambda: client)


class TestChClientTls:
    """The shim's raw client takes the engine's ClickHouseSettings TLS posture."""

    def test_secure_settings_pass_verify_and_ca_cert(self, tmp_path):
        ca = tmp_path / "internal-ca.pem"
        ca.write_text("cert")
        settings = DFESettings(env="test")
        settings.clickhouse.secure = True
        settings.clickhouse.verify = True
        settings.clickhouse.ca_cert = str(ca)

        with patch("clickhouse_connect.get_client") as get_client:
            _ch_client(settings)
        kwargs = get_client.call_args[1]
        assert kwargs["secure"] is True
        assert kwargs["verify"] is True
        assert kwargs["ca_cert"] == str(ca)

    def test_insecure_settings_pass_no_tls_kwargs(self):
        settings = DFESettings(env="test")
        settings.clickhouse.secure = False

        with patch("clickhouse_connect.get_client") as get_client:
            _ch_client(settings)
        kwargs = get_client.call_args[1]
        assert "secure" not in kwargs
        assert "verify" not in kwargs
        assert "ca_cert" not in kwargs


def test_pressure_returns_value():
    shim = _shim(_FakeClient(rows=[[42]]))
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42


def test_pressure_clamped_to_band():
    shim = _shim(_FakeClient(rows=[[150]]))
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 100  # clamp_max 100


def test_pressure_matches_the_gauge_under_any_namespace_prefix():
    """One pinned name read one app and left the trigger inert everywhere else."""
    client = _FakeClient(rows=[[42]])
    shim = _shim(client)

    shim.run("pressure", {"service": "dfe-archiver"})

    sql = client.calls[0][0]
    assert "dfe_scaling_pressure" not in sql
    assert "MetricName = 'scaling_pressure'" in sql
    assert "endsWith(MetricName, '_scaling_pressure')" in sql


def test_pressure_reduces_each_series_before_taking_the_max():
    """An app that tags some rows and not others must not average itself down."""
    client = _FakeClient(rows=[[90]])
    shim = _shim(client)

    assert shim.run("pressure", {"service": "dfe-fetcher"}) == 90

    sql = client.calls[0][0]
    assert "GROUP BY MetricName, cityHash64(Attributes)" in sql
    assert "max(series)" in sql


def test_failsafe_holds_last_good():
    client = _FakeClient(rows=[[42]])
    shim = QueryShim(DFESettings(env="test"), client_factory=lambda: client)
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42  # caches 42
    client.error = RuntimeError("CH down")
    # A failure returns the cached last-good, NOT an error and NOT a scale-up.
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42


def test_failsafe_cold_hold_when_no_cache():
    shim = _shim(_FakeClient(error=RuntimeError("CH down")))
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 0  # cold_hold


def test_injection_param_rejected_and_failsafe():
    client = _FakeClient(rows=[[42]])
    shim = QueryShim(DFESettings(env="test"), client_factory=lambda: client)
    # A malformed service never reaches ClickHouse and degrades to cold_hold.
    assert shim.run("pressure", {"service": "x'; DROP TABLE t--"}) == 0
    assert client.calls == []


def test_backlog_uses_due_query():
    client = _FakeClient(rows=[[7]])
    shim = _shim(client)
    assert shim.run("backlog") == 7
    sql = client.calls[0][0]
    assert "hunt_schedule" in sql  # proves it ran schedule.due_query, not a copy


def test_unknown_query_raises_keyerror():
    shim = _shim(_FakeClient())
    with pytest.raises(KeyError):
        shim.run("nope")


def test_query_names_lists_builtins():
    shim = _shim(_FakeClient())
    assert shim.query_names == ["backlog", "pressure"]


def _overridden_shim(client: _FakeClient, tmp_path, override: str) -> QueryShim:
    path = tmp_path / "queries.yaml"
    path.write_text(override, encoding="utf-8")
    settings = DFESettings(env="test", keda_shim={"query_config": str(path)})
    return QueryShim(settings, client_factory=lambda: client)


def test_an_override_params_list_replaces_the_builtin_one(tmp_path):
    """A rewritten query binds exactly the params its override names."""
    client = _FakeClient(rows=[[42]])
    shim = _overridden_shim(
        client,
        tmp_path,
        "queries:\n"
        "  pressure:\n"
        "    sql: SELECT count() FROM __DB__.t WHERE app = {app:String}\n"
        "    params: [app]\n",
    )

    assert shim.run("pressure", {"app": "dfe-receiver"}) == 42
    assert client.calls[0][1] == {"window_seconds": 60, "app": "dfe-receiver"}


def test_an_override_adds_a_query_beside_the_builtins(tmp_path):
    shim = _overridden_shim(
        _FakeClient(),
        tmp_path,
        "queries:\n  spool:\n    database: data\n    sql: SELECT 1\n",
    )

    assert shim.query_names == ["backlog", "pressure", "spool"]
