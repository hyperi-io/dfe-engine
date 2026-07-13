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

from __future__ import annotations

from fastapi import FastAPI, Request
from scalo.health import HealthManager, create_health_router

from dfe_engine.settings import DFESettings, load_settings

from .shim import QueryShim


def create_app(
    settings: DFESettings | None = None,
    shim: QueryShim | None = None,
) -> FastAPI:
    """Build the shim's FastAPI app. ``shim`` is injectable for tests."""
    settings = settings or load_settings()
    shim = shim or QueryShim(settings)
    health = HealthManager()

    app = FastAPI(title="dfe-keda-shim", docs_url=None, redoc_url=None)
    app.state.shim = shim
    app.state.health_manager = health
    app.include_router(create_health_router(health), include_in_schema=False)

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
