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

import os

import typer
import uvicorn
from scalo.cli import CommonArgs

from dfe_engine.settings import load_settings

from .app import create_app

app = typer.Typer(help="dfe-keda-shim: config-driven ClickHouse -> KEDA metrics-api adapter.")

_SERVICE_NAME = "dfe-keda-shim"
# The prefix the dfe-engine daemon hands scalo, so every process on the image reads one cascade.
_ENV_PREFIX = "DFE_API"


def _init_logger() -> None:
    """Install scalo's logger sinks, in the order the ``dfe-engine`` daemon does.

    A plain Typer app gets nothing from scalo's ServiceApp, so without this call
    every line goes through loguru's bare default handler: no scalo format, no
    secret scrubbing, no keyword fields. ``LOG_LEVEL`` and ``LOG_FORMAT`` fill
    the slots the daemon's flags read them into.

    Raises:
        scalo.cli.error.ConfigError: If the config cascade cannot be loaded.
        scalo.cli.error.LoggerError: If the logger cannot be initialised.
    """
    args = CommonArgs(
        log_level=os.environ.get("LOG_LEVEL"),
        log_format=os.environ.get("LOG_FORMAT"),
    )
    config = args.load_config(_ENV_PREFIX)
    args.init_logger(config=config, service_name=_SERVICE_NAME)


# No app.callback(): with one command Typer runs it bare, and the chart invokes it with no args.
@app.command("run")
def run() -> None:
    """Serve the shim until SIGTERM (k8s pod stop) / SIGINT (local ctrl-c)."""
    _init_logger()
    settings = load_settings()
    ks = settings.keda_shim
    uvicorn.Server(
        uvicorn.Config(create_app(settings=settings), host=ks.host, port=ks.port, log_level="info")
    ).run()


def main() -> None:
    """Console-script entrypoint (``dfe-keda-shim``)."""
    app()
