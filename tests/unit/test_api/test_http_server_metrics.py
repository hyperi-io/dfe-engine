#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_http_server_metrics.py
#  Purpose:      The API records per-request metrics under its route templates
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine's HTTP server histogram, through the real middleware stack.

The status page reads request rate, error rate and p95 for the engine from this
histogram, so it must be outermost and see FastAPI's route template.
"""

import httpx
from scalo.metrics import create_metrics
from scalo.metrics.http_server import HttpServerMetricsMiddleware

from dfe_engine.api.app import create_app


def test_the_histogram_middleware_is_outermost(api_settings):
    app = create_app(
        metrics_manager=create_metrics("engine-http-outermost", backend="prometheus"),
        settings=api_settings,
    )

    assert app.user_middleware[0].cls is HttpServerMetricsMiddleware


def test_no_metrics_manager_adds_no_histogram_middleware(api_settings):
    app = create_app(settings=api_settings)

    assert all(
        middleware.cls is not HttpServerMetricsMiddleware for middleware in app.user_middleware
    )


async def test_a_request_is_recorded_under_its_route_template(api_settings):
    metrics = create_metrics("engine-http-template", backend="prometheus")
    app = create_app(metrics_manager=metrics, settings=api_settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(base_url="http://test", transport=transport) as client:
        await client.get("/api/v1/apps/dfe-loader/default/status")

    # The API is mounted at /api/v1, and FastAPI's template is relative to the mount.
    exposition = metrics.get_metrics().decode()
    assert 'endpoint="/apps/{service}/{instance}/status"' in exposition
    assert "dfe-loader/default" not in exposition
