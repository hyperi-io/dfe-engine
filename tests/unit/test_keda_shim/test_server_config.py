#  Project:      dfe-engine
#  File:         tests/unit/test_keda_shim/test_server_config.py
#  Purpose:      The shim pod's uvicorn writes no access line for a KEDA poll
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The config ``dfe-keda-shim run`` serves with, through a real uvicorn server.

KEDA polls the shim every few seconds, so uvicorn's access line would write each
poll's full URL at INFO. A small app stands in for the shim app, which needs a
process of its own for its metrics (``tests/support/keda_shim_metrics_probe.py``).
"""

from fastapi import FastAPI

from dfe_engine.keda_shim.cli import shim_server_config
from dfe_engine.settings import DFESettings
from tests.support.uvicorn_access import serve_once, uvicorn_access_lines

_POLL = "/q/source-ingest-lag?source=acme-firewall"


def _poll_app() -> FastAPI:
    app = FastAPI()

    @app.get("/q/{name}")
    async def query(name: str, source: str = "") -> dict[str, float]:
        return {"value": 0.0}

    return app


def test_a_poll_writes_no_uvicorn_access_line():
    with uvicorn_access_lines() as lines:
        status = serve_once(
            lambda _port: shim_server_config(_poll_app(), DFESettings(env="test")), _POLL
        )

    assert status == 200
    assert lines == []


def test_the_shim_still_reads_its_client_from_a_forwarding_peer():
    config = shim_server_config(_poll_app(), DFESettings(env="test"))

    assert config.proxy_headers is True
    assert config.access_log is False
