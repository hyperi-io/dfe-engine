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

from __future__ import annotations

import typer
import uvicorn

from dfe_engine.settings import load_settings

from .app import create_app

app = typer.Typer(help="dfe-keda-shim: config-driven ClickHouse -> KEDA metrics-api adapter.")


@app.command("run")
def run() -> None:
    """Serve the shim until SIGTERM (k8s pod stop) / SIGINT (local ctrl-c)."""
    settings = load_settings()
    ks = settings.keda_shim
    uvicorn.Server(
        uvicorn.Config(create_app(settings=settings), host=ks.host, port=ks.port, log_level="info")
    ).run()


def main() -> None:
    """Console-script entrypoint (``dfe-keda-shim``)."""
    app()
