#  Project:      dfe-engine
#  File:         keda_shim/cli.py
#  Purpose:      dfe-keda-shim console entrypoint (serves the KEDA metrics-api shim)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""`dfe-keda-shim` - serve the config-driven ClickHouse -> KEDA metrics-api adapter.

Deployed by dfe-infra as its OWN small pod (on the engine image, but a separate
Deployment) so the engine API is never on the scaling hot path. It builds the shim
app from the standard settings cascade and serves it with uvicorn until SIGTERM.
"""

import typer
import uvicorn
from fastapi import FastAPI
from scalo.metrics import create_metrics
from scalo.metrics.groups import ResourceMetrics

from dfe_engine.api import init_scalo_logger
from dfe_engine.settings import DFESettings, load_settings

from .app import create_app

app = typer.Typer(help="dfe-keda-shim: config-driven ClickHouse -> KEDA metrics-api adapter.")

_SERVICE_NAME = "dfe-keda-shim"


def served_app(settings: DFESettings) -> FastAPI:
    """Build the app the pod serves, with process and request metrics on its own manager.

    Args:
        settings: The settings the shim reads its ClickHouse and query config from.

    Returns:
        The shim app, recording each request on a manager that also carries the
        process and container gauges.
    """
    # Unprefixed, as on the daemon: dashboards read process_* and http.server.request.duration.
    metrics = create_metrics(_SERVICE_NAME)
    ResourceMetrics(mgr=metrics)
    return create_app(settings=settings, metrics_manager=metrics)


# No app.callback(): with one command Typer runs it bare, and the chart invokes it with no args.
@app.command("run")
def run() -> None:
    """Serve the shim until SIGTERM (k8s pod stop) / SIGINT (local ctrl-c)."""
    init_scalo_logger(_SERVICE_NAME, otel_tracing=True)
    settings = load_settings()
    ks = settings.keda_shim
    uvicorn.Server(
        uvicorn.Config(
            served_app(settings),
            host=ks.host,
            port=ks.port,
            log_level="info",
            # None keeps uvicorn's loggers on scalo's handlers, which scrub tokens from access lines.
            log_config=None,
        )
    ).run()


def main() -> None:
    """Console-script entrypoint (``dfe-keda-shim``)."""
    app()
