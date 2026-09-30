#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_source_signals.py
#  Purpose:      GET /api/v1/sources/{name}/signals - is anything arriving
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Per-source throughput, read off the loader's per-table counter.

The console puts a number beside a source so an operator can see whether it is
carrying records without leaving the page. A null is the honest answer to "no
series here" and the console hides it; a zero would claim a measurement nobody
took, which is why every reading is nullable and the route still answers 200.
"""

from __future__ import annotations

import time

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

LOADER = "dfe-loader"
SIGNALS = "/api/v1/sources/test-source/signals"


class FakeClickHouse:
    """Returns one canned result set and records what it was asked."""

    def __init__(self, rows: list[tuple] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql: str, parameters: dict | None = None, settings: dict | None = None):
        self.calls.append((sql, dict(parameters or {})))
        if self.fail:
            raise RuntimeError("clickhouse is down")
        return self.rows


def _wire(app, tmp_path) -> GitCrud:
    """Attach a real local deploy repo, as the app-management tests do."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = "kubernetes"
    gc = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _deploy_loader(client, headers, instance: str = "main") -> None:
    resp = client.post(
        f"/api/v1/apps/{LOADER}/instances", json={"instance": instance}, headers=headers
    )
    assert resp.status_code in (200, 201), resp.text


def _define(client, headers, sample_source: dict) -> None:
    assert client.post("/api/v1/sources", json=sample_source, headers=headers).status_code == 201


def _series(*points: tuple[float, float]) -> list[tuple]:
    """A counter series as ClickHouse returns it: (TimeUnix, summed value)."""
    return [(ts, value) for ts, value in points]


@pytest.fixture
def now() -> float:
    return time.time()


class TestSourceSignals:
    def test_a_rising_counter_becomes_records_a_minute(
        self, client, app, admin_headers, tmp_path, sample_source, now
    ):
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers)
        _define(client, admin_headers, sample_source)
        _use(app, FakeClickHouse(_series((now - 120, 100.0), (now - 60, 220.0), (now, 340.0))))

        body = client.get(SIGNALS, headers=admin_headers).json()

        assert body["source"] == "test-source"
        assert body["table"] == "test-source"
        assert body["landed_in_default"] is False
        assert body["window_seconds"] == 300
        # 240 records over 120 seconds.
        assert body["records_per_min"] == pytest.approx(120.0)
        assert body["last_seen"] is not None

    def test_the_query_is_bound_to_the_table_and_the_deployed_loaders(
        self, client, app, admin_headers, tmp_path, sample_source, now
    ):
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers, "main")
        _define(client, admin_headers, sample_source)
        ch = FakeClickHouse(_series((now - 60, 1.0), (now, 2.0)))
        _use(app, ch)

        client.get(SIGNALS, headers=admin_headers)

        sql, params = ch.calls[0]
        assert "otel_metrics_sum" in sql
        assert "loader_messages_by_table_total" in sql
        assert params["table"] == "test-source"
        # A stack-wide app's chart hard-codes OTEL_SERVICE_NAME to the service, so
        # the instance name never reaches the query.
        assert params["services"] == [LOADER]
        # Bounded, so the console asking on every list render cannot scan the table.
        assert params["window_seconds"] == 300

    def test_an_absent_series_is_null_and_still_200(
        self, client, app, admin_headers, tmp_path, sample_source
    ):
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers)
        _define(client, admin_headers, sample_source)
        _use(app, FakeClickHouse([]))

        resp = client.get(SIGNALS, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["records_per_min"] is None
        assert resp.json()["last_seen"] is None

    def test_a_flat_counter_reports_a_rate_but_no_arrival(
        self, client, app, admin_headers, tmp_path, sample_source, now
    ):
        # The loader keeps reporting the same total after traffic stops, so a
        # present series is not itself an arrival.
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers)
        _define(client, admin_headers, sample_source)
        _use(app, FakeClickHouse(_series((now - 120, 500.0), (now, 500.0))))

        body = client.get(SIGNALS, headers=admin_headers).json()

        assert body["records_per_min"] == 0.0
        assert body["last_seen"] is None

    def test_a_counter_reset_reports_nothing_rather_than_a_negative_rate(
        self, client, app, admin_headers, tmp_path, sample_source, now
    ):
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers)
        _define(client, admin_headers, sample_source)
        _use(app, FakeClickHouse(_series((now - 120, 900.0), (now, 10.0))))

        assert client.get(SIGNALS, headers=admin_headers).json()["records_per_min"] is None

    def test_no_deployed_loader_asks_clickhouse_nothing(
        self, client, app, admin_headers, tmp_path, sample_source
    ):
        _wire(app, tmp_path)
        _define(client, admin_headers, sample_source)
        ch = FakeClickHouse(_series((1.0, 1.0), (2.0, 2.0)))
        _use(app, ch)

        body = client.get(SIGNALS, headers=admin_headers).json()

        assert ch.calls == []
        assert body["records_per_min"] is None

    def test_a_fetcher_source_on_the_main_topic_reports_the_shared_table(
        self, client, app, admin_headers, tmp_path
    ):
        # Its records carry _source = main, so the loader counts them against the
        # landing table every unmatched record shares -- and the rate is then that
        # table's, not this source's.
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers)
        client.post(
            "/api/v1/sources",
            json={
                "source": "crates",
                "fetcher": {
                    "source_type": "crates_io",
                    "topic": "main",
                    "config": {"crates": ["dfe-fetcher"]},
                },
            },
            headers=admin_headers,
        )
        _use(app, FakeClickHouse([]))

        body = client.get("/api/v1/sources/crates/signals", headers=admin_headers).json()

        assert body["table"] == "main"
        assert body["landed_in_default"] is True

    def test_the_main_flow_answers_before_anyone_has_written_it(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _use(app, FakeClickHouse([]))

        resp = client.get("/api/v1/sources/main/signals", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["landed_in_default"] is True

    def test_an_unknown_source_is_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _use(app, FakeClickHouse([]))

        assert client.get("/api/v1/sources/ghost/signals", headers=admin_headers).status_code == 404

    def test_a_broken_database_is_503_not_a_wrong_number(
        self, client, app, admin_headers, tmp_path, sample_source
    ):
        _wire(app, tmp_path)
        _deploy_loader(client, admin_headers)
        _define(client, admin_headers, sample_source)
        _use(app, FakeClickHouse(fail=True))

        resp = client.get(SIGNALS, headers=admin_headers)

        assert resp.status_code == 503
        assert resp.json()["code"] == "metrics_unavailable"

    def test_a_viewer_can_read_it(
        self, client, app, viewer_headers, admin_headers, tmp_path, sample_source
    ):
        _wire(app, tmp_path)
        _define(client, admin_headers, sample_source)
        _use(app, FakeClickHouse([]))

        assert client.get(SIGNALS, headers=viewer_headers).status_code == 200

    def test_anonymous_is_refused(self, client, app, tmp_path):
        _wire(app, tmp_path)
        _use(app, FakeClickHouse([]))

        assert client.get(SIGNALS).status_code == 401


def _use(app, ch: FakeClickHouse) -> None:
    """Answer the ClickHouse dependency with the fake for this test."""
    from dfe_engine.api.deps import get_clickhouse_client

    app.dependency_overrides[get_clickhouse_client] = lambda: ch
