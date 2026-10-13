#  Project:      dfe-engine
#  File:         keda_shim/app.py
#  Purpose:      FastAPI app for the dfe-keda-shim (KEDA metrics-api endpoints)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""FastAPI surface for the KEDA shim.

Every endpoint returns ``{"value": <int>}`` so a KEDA ``metrics-api`` trigger uses a
single ``valueLocation: value`` regardless of query. ``/q/{name}`` is the generic
route; ``/keda/pressure`` and ``/keda/hunt-backlog`` are readable aliases for the two
built-in queries. Nothing here 5xx's on a metric outage - the shim's fail-safe turns
that into a held value (KEDA treats a 5xx as an error and may apply its own fallback).
"""

from fastapi import FastAPI, Request
from scalo.health import HealthManager, create_health_router
from scalo.metrics import MetricsManager
from scalo.metrics.http_server import HttpServerMetricsMiddleware

from dfe_engine.fastapi_telemetry import FASTAPI_TELEMETRY
from dfe_engine.settings import DFESettings, load_settings

from .shim import QueryShim


def create_app(
    settings: DFESettings | None = None,
    shim: QueryShim | None = None,
    metrics_manager: MetricsManager | None = None,
) -> FastAPI:
    """Build the shim's FastAPI app.

    Args:
        settings: DFE settings. Defaults to ``load_settings()``.
        shim: The query runner the routes call. Injectable for tests.
        metrics_manager: The scalo ``MetricsManager`` each request's duration is
            recorded on. Left ``None``, no request is recorded.

    Returns:
        The configured FastAPI application.
    """
    settings = settings or load_settings()
    shim = shim or QueryShim(settings)
    health = HealthManager()

    app = FastAPI(
        title="dfe-keda-shim",
        docs_url=None,
        redoc_url=None,
        # scalo's middleware below records each request, and FastAPI's own would count it twice.
        telemetry={**FASTAPI_TELEMETRY, "metrics": False},
    )
    app.state.shim = shim
    app.state.health_manager = health
    app.include_router(create_health_router(health), include_in_schema=False)
    if metrics_manager is not None:
        app.add_middleware(HttpServerMetricsMiddleware, metrics=metrics_manager)

    @app.get("/q/{name}")
    def query(name: str, request: Request) -> dict:
        params = dict(request.query_params)
        try:
            value = shim.run(name, params)
        except KeyError:
            # Unknown query NAME is a config error, not a metric outage. Return a
            # safe 0 (hold-at-min) rather than 5xx, so a typo can never scale up.
            value = 0
        return {"value": value}

    @app.get("/keda/pressure")
    def pressure(service: str) -> dict:
        return {"value": shim.run("pressure", {"service": service})}

    @app.get("/keda/hunt-backlog")
    def hunt_backlog() -> dict:
        return {"value": shim.run("backlog")}

    health.set_started()
    health.set_ready()
    return app
