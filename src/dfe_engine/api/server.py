#  Project:      dfe-engine
#  File:         api/server.py
#  Purpose:      The uvicorn server dfe-engine run serves on
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The uvicorn server ``dfe-engine run`` serves on."""

import os
from types import FrameType
from typing import Any, override

import uvicorn

from dfe_engine.startup_stop import main_thread_waiting


def server_config(app: Any, *, host: str, port: int, proxy_headers: bool = False) -> uvicorn.Config:
    """The uvicorn config every server on the engine image serves its app with.

    uvicorn's access line is off because it writes the request path, values and all.
    The engine app's ``AccessLogMiddleware`` writes the line with the route template
    instead.

    Args:
        app: The ASGI app to serve.
        host: The address to listen on.
        port: The port to listen on.
        proxy_headers: Let uvicorn read the client and scheme from a forwarding peer.
            Off for the engine API, whose app resolves them from
            ``api.forwarded_allow_ips`` itself.

    Returns:
        The config, with uvicorn's loggers left on scalo's handlers.
    """
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        # None keeps uvicorn's loggers on scalo's handlers, which scrub tokens from its lines.
        log_config=None,
        access_log=False,
        proxy_headers=proxy_headers,
    )


class DaemonServer(uvicorn.Server):
    """uvicorn's server, which a stop signal also ends while startup waits.

    uvicorn records a SIGTERM or SIGINT and acts on it once startup returns. One that
    lands inside a :func:`~dfe_engine.startup_stop.stoppable_wait` ends the process at
    once with status 128 plus the signal number, the status a shell gives a process
    that signal ended. Anywhere else the stop waits for startup and drains as uvicorn's
    does.
    """

    # Called by uvicorn's signal capture, so vulture sees no caller.
    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:  # noqa: V105
        if not self.started and main_thread_waiting():
            # PID 1 ignores a signal re-raised under its default action, so exit outright.
            os._exit(128 + sig)
        super().handle_exit(sig, frame)
