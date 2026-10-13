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

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

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
        self.closed = False

    def query(self, sql, parameters=None, settings=None):
        self.calls.append((sql, parameters, settings))
        if self.error is not None:
            raise self.error
        return _FakeResult(self.rows)

    def close(self) -> None:
        self.closed = True


class _CountingFactory:
    """Builds a fresh client per call and keeps every client it built."""

    def __init__(self, error: Exception | None = None, delay: float = 0.0) -> None:
        self.error = error
        self.delay = delay
        self.built: list[_FakeClient] = []
        self._lock = threading.Lock()

    def __call__(self) -> _FakeClient:
        time.sleep(self.delay)
        client = _FakeClient(rows=[[42]], error=self.error)
        with self._lock:
            self.built.append(client)
        return client

    @property
    def open(self) -> list[_FakeClient]:
        return [client for client in self.built if not client.closed]


def _shim(client: _FakeClient) -> QueryShim:
    return QueryShim(DFESettings(env="test"), client_factory=lambda: client)


class _ConnectRecorder:
    """Takes clickhouse_connect.get_client's place and keeps the keyword arguments it got."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.client = object()

    def __call__(self, **kwargs: Any) -> object:
        self.kwargs = kwargs
        return self.client


class TestChClientTls:
    """The shim's raw client takes the engine's ClickHouseSettings TLS posture."""

    def test_secure_settings_pass_verify_and_ca_cert(self, tmp_path):
        ca = tmp_path / "internal-ca.pem"
        ca.write_text("cert")
        settings = DFESettings(env="test")
        settings.clickhouse.secure = True
        settings.clickhouse.verify = True
        settings.clickhouse.ca_cert = str(ca)
        connect = _ConnectRecorder()

        assert _ch_client(settings, connect=connect) is connect.client
        assert connect.kwargs["secure"] is True
        assert connect.kwargs["verify"] is True
        assert connect.kwargs["ca_cert"] == str(ca)

    def test_insecure_settings_pass_no_tls_kwargs(self):
        settings = DFESettings(env="test")
        settings.clickhouse.secure = False
        connect = _ConnectRecorder()

        _ch_client(settings, connect=connect)

        assert connect.kwargs["host"] == settings.clickhouse.host
        assert "secure" not in connect.kwargs
        assert "verify" not in connect.kwargs
        assert "ca_cert" not in connect.kwargs


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


def test_pressure_keeps_each_pod_a_series_of_its_own():
    """Pushed rows carry the pod in the resource, so a key without it averages the pods."""
    client = _FakeClient(rows=[[80]])
    shim = _shim(client)

    shim.run("pressure", {"service": "dfe-receiver"})

    group_by = client.calls[0][0].split("GROUP BY", 1)[1]
    assert "ResourceAttributes['service.instance.id']" in group_by


def test_a_missing_gauge_reads_zero_rather_than_the_last_good_value():
    """No rows is a successful answer, so it replaces the cache instead of holding it."""
    client = _FakeClient(rows=[[42]])
    shim = _shim(client)
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42

    client.rows = []

    assert shim.run("pressure", {"service": "dfe-receiver"}) == 0


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


def test_undeclared_params_add_no_cache_entries():
    """/q/{name} hands the shim every request param, so only declared ones may key the cache."""
    shim = _shim(_FakeClient(rows=[[42]]))

    for n in range(500):
        assert shim.run("pressure", {"service": "dfe-receiver", f"junk{n}": str(n)}) == 42

    assert len(shim._cache) == 1


def test_an_undeclared_param_does_not_hide_the_last_good_value():
    client = _FakeClient(rows=[[42]])
    shim = _shim(client)
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42
    client.error = RuntimeError("CH down")

    assert shim.run("pressure", {"service": "dfe-receiver", "cachebust": "1"}) == 42


def test_answers_equal_to_cold_hold_add_no_cache_entries():
    """A miss already returns cold_hold, so a held copy of it only grows the cache."""
    shim = _shim(_FakeClient(rows=[[0]]))

    for n in range(500):
        assert shim.run("pressure", {"service": f"svc-{n}"}) == 0

    assert shim._cache == {}


def test_an_answer_other_than_cold_hold_is_still_held_among_cold_hold_ones():
    client = _FakeClient(rows=[[0]])
    shim = _shim(client)
    for n in range(500):
        shim.run("pressure", {"service": f"svc-{n}"})
    client.rows = [[42]]
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42
    client.error = RuntimeError("CH down")

    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42
    assert len(shim._cache) == 1


def test_a_cold_hold_answer_replaces_the_value_held_before_it():
    """A later failure must return cold_hold, not the stale value the answer superseded."""
    client = _FakeClient(rows=[[42]])
    shim = _shim(client)
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42
    client.rows = [[0]]
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 0
    client.error = RuntimeError("CH down")

    assert shim.run("pressure", {"service": "dfe-receiver"}) == 0
    assert shim._cache == {}


def test_cold_hold_is_the_querys_own_value_not_zero(tmp_path):
    """With a cold_hold of 5, a 0 answer is real information and is held."""
    client = _FakeClient(rows=[[0]])
    shim = _overridden_shim(client, tmp_path, "queries:\n  pressure:\n    cold_hold: 5\n")
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 0
    client.error = RuntimeError("CH down")
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 0

    client.error = None
    client.rows = [[5]]
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 5
    assert shim._cache == {}


def test_every_client_a_failed_query_discards_is_closed():
    """A TLS client dropped unclosed keeps its pool and socket for the life of the process."""
    factory = _CountingFactory(error=RuntimeError("CH down"))
    shim = QueryShim(DFESettings(env="test"), client_factory=factory)

    for _ in range(50):
        assert shim.run("pressure", {"service": "dfe-receiver"}) == 0

    assert len(factory.built) == 50
    assert factory.open == []


def test_polls_racing_to_build_the_first_client_leave_one_open():
    """KEDA polls every trigger on one tick, so first polls overlap on an empty shim."""
    factory = _CountingFactory(delay=0.05)
    shim = QueryShim(DFESettings(env="test"), client_factory=factory)
    start = threading.Barrier(8)

    def poll(_: int) -> int:
        start.wait()
        return shim.run("pressure", {"service": "dfe-receiver"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(poll, range(8)))

    assert values == [42] * 8
    assert len(factory.open) == 1


def test_a_rejected_param_keeps_the_healthy_client():
    """A malformed request never reached ClickHouse, so the connection is not suspect."""
    factory = _CountingFactory()
    shim = QueryShim(DFESettings(env="test"), client_factory=factory)

    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42
    assert shim.run("pressure", {"service": "not an identifier"}) == 0
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42

    assert len(factory.built) == 1
    assert factory.open == factory.built


def test_injection_param_rejected_and_failsafe():
    client = _FakeClient(rows=[[42]])
    shim = QueryShim(DFESettings(env="test"), client_factory=lambda: client)
    # A malformed service never reaches ClickHouse and degrades to cold_hold.
    assert shim.run("pressure", {"service": "x'; DROP TABLE t--"}) == 0
    assert client.calls == []


def test_a_param_ending_in_a_newline_is_rejected_like_any_other_malformed_one():
    client = _FakeClient(rows=[[42]])
    shim = QueryShim(DFESettings(env="test"), client_factory=lambda: client)

    assert shim.run("pressure", {"service": "dfe-receiver\n"}) == 0
    assert client.calls == []
    assert shim.run("pressure", {"service": "dfe-receiver"}) == 42


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
