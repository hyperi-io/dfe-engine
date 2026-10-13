#  Project:      dfe-engine
#  File:         tests/unit/test_keda_shim/test_app.py
#  Purpose:      Endpoint tests for the KEDA shim FastAPI app (stub shim, no CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Endpoint tests for the KEDA shim app - every route returns {"value": <int>}."""

from fastapi.testclient import TestClient
from scalo.metrics import create_metrics
from scalo.metrics.http_server import HttpServerMetricsMiddleware

from dfe_engine.keda_shim.app import create_app
from dfe_engine.settings import DFESettings


class _StubShim:
    def __init__(self, values: dict[str, int]) -> None:
        self.values = values
        self.calls: list[tuple] = []

    def run(self, name: str, params=None) -> int:
        self.calls.append((name, params))
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]


def _client(values: dict[str, int]) -> TestClient:
    return TestClient(create_app(settings=DFESettings(env="test"), shim=_StubShim(values)))


def test_pressure_alias():
    resp = _client({"pressure": 55}).get("/keda/pressure", params={"service": "dfe-receiver"})
    assert resp.status_code == 200
    assert resp.json() == {"value": 55}


def test_hunt_backlog_alias():
    assert _client({"backlog": 3}).get("/keda/hunt-backlog").json() == {"value": 3}


def test_generic_query_route():
    assert _client({"pressure": 55}).get("/q/pressure", params={"service": "x"}).json() == {
        "value": 55
    }


def test_unknown_query_returns_safe_zero():
    # Stub raises KeyError for any name -> route degrades to hold-at-min (0).
    assert _client({}).get("/q/does-not-exist").json() == {"value": 0}


def test_health_live():
    # /livez, not the /health/live alias scalo retired in 2.29.12.
    assert _client({}).get("/livez").status_code == 200


def test_retired_health_alias_is_gone():
    # An alias that keeps answering 200 hides a probe still aimed at it.
    assert _client({}).get("/health/live").status_code == 404


def test_a_metrics_manager_puts_the_request_histogram_outermost():
    app = create_app(
        settings=DFESettings(env="test"),
        metrics_manager=create_metrics("keda-shim-outermost", backend="prometheus"),
    )

    assert app.user_middleware[0].cls is HttpServerMetricsMiddleware


def test_no_metrics_manager_adds_no_histogram_middleware():
    app = create_app(settings=DFESettings(env="test"))

    assert all(
        middleware.cls is not HttpServerMetricsMiddleware for middleware in app.user_middleware
    )
