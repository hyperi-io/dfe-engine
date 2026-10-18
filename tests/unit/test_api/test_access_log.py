#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_access_log.py
#  Purpose:      The API access line names the route and status, never a username in the path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The access line at INFO carries the route template and the status, not the path's values.

Once through the engine's app, against an account route whose path is the user's
login, and once through a real uvicorn server on the daemon's config, which turns
uvicorn's path-carrying access line off.
"""

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from scalo.logger import logger
from scalo.metrics.http_server import UNMATCHED_ROUTE

from dfe_engine.api.middleware.access_log import ACCESS_LINE, AccessLogMiddleware
from dfe_engine.api.server import server_config

_LOGIN = "jane.doe@example.com"
_LOCAL_PART = "jane.doe"


@contextmanager
def access_lines() -> Generator[list[dict]]:
    """The engine's access lines written inside the block, as their level and fields."""
    records: list[dict] = []

    def keep(message) -> None:
        record = message.record
        if record["message"] == ACCESS_LINE:
            records.append({"level": record["level"].name, **record["extra"]})

    sink = logger.add(keep, level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink)


def test_an_account_lookup_logs_its_route_and_status_without_the_login(client, admin_headers):
    with access_lines() as lines:
        response = client.get(f"/api/v1/auth/accounts/{_LOGIN}", headers=admin_headers)

    assert response.status_code == 404
    assert lines == [
        {
            "level": "INFO",
            "method": "GET",
            "route": "/api/v1/auth/accounts/{username}",
            "status": 404,
            "duration_ms": lines[0]["duration_ms"],
            "client": lines[0]["client"],
        }
    ]
    assert _LOCAL_PART not in repr(lines)


def test_a_path_no_route_matches_is_logged_as_unmatched(client, admin_headers):
    with access_lines() as lines:
        client.get(f"/api/v1/no-such-route/{_LOGIN}", headers=admin_headers)

    assert [(line["route"], line["status"]) for line in lines] == [(UNMATCHED_ROUTE, 404)]
    assert _LOCAL_PART not in repr(lines)


class _Records(logging.Handler):
    """Every stdlib record from one logger that reaches the root, formatted."""

    def __init__(self, name: str) -> None:
        super().__init__(level=logging.DEBUG)
        self._name = name
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == self._name:
            self.lines.append(record.getMessage())


@contextmanager
def uvicorn_access_as_scalo_leaves_it() -> Generator[_Records]:
    """``uvicorn.access`` with no handler of its own, propagating to a root handler.

    That is how scalo's ``setup()`` leaves it, so its records reach scalo's sinks. uvicorn
    writes its access line only while the logger has a handler to reach, on its own or
    through propagation.
    """
    access = logging.getLogger("uvicorn.access")
    saved = (list(access.handlers), access.propagate, access.level)
    root = logging.getLogger()
    records = _Records("uvicorn.access")
    access.handlers.clear()
    access.propagate = True
    root.addHandler(records)
    try:
        yield records
    finally:
        root.removeHandler(records)
        access.handlers[:] = saved[0]
        access.propagate = saved[1]
        access.setLevel(saved[2])


def _accounts_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/auth/accounts/{username}")
    async def account(username: str) -> dict[str, int]:
        return {"length": len(username)}

    app.add_middleware(AccessLogMiddleware)
    return app


def _serve_one_lookup(config_for) -> tuple[int, list[str], list[dict]]:
    """Serve the accounts app on *config_for*'s config and look one login up.

    Returns:
        The status, uvicorn's access lines and the engine's.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    with uvicorn_access_as_scalo_leaves_it() as uvicorn_access, access_lines() as lines:
        server = uvicorn.Server(config_for(_accounts_app(), port))
        thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])))
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "uvicorn did not start"
            status = httpx.get(f"http://127.0.0.1:{port}/api/v1/auth/accounts/{_LOGIN}").status_code
        finally:
            server.should_exit = True
            thread.join(10.0)
            listener.close()
    return status, uvicorn_access.lines, lines


def test_the_daemon_config_writes_no_uvicorn_access_line_with_the_path():
    status, uvicorn_lines, lines = _serve_one_lookup(
        lambda app, port: server_config(app, host="127.0.0.1", port=port)
    )

    assert status == 200
    assert uvicorn_lines == []
    assert [(line["route"], line["status"]) for line in lines] == [
        ("/api/v1/auth/accounts/{username}", 200)
    ]
    assert _LOCAL_PART not in repr(lines)


def test_uvicorns_own_access_line_carries_the_login_it_is_turned_off_for():
    """The control: the same server with uvicorn's access line on writes the path whole."""
    status, uvicorn_lines, _lines = _serve_one_lookup(
        lambda app, port: uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
    )

    assert status == 200
    assert len(uvicorn_lines) == 1
    assert _LOCAL_PART in uvicorn_lines[0]
