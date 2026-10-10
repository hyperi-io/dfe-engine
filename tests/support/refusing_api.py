#  Project:      dfe-engine
#  File:         tests/support/refusing_api.py
#  Purpose:      Local HTTP server that refuses every request with one status and JSON body
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP server that refuses every request with one status and JSON body."""

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tests.support.loopback import stop_server


def _handler_for(*, api: LocalRefusingApi) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers with the refusal set on ``api``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Record the request path and answer with the refusal."""
            api.paths.append(self.path)
            encoded = json.dumps(api.body).encode("utf-8")
            self.send_response(api.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalRefusingApi:
    """An identity provider API stand-in on 127.0.0.1 that answers every GET with one refusal.

    ``status`` and ``body`` are the answer. ``paths`` records the path and query of
    each request as the client sent it.
    """

    def __init__(self, *, body: dict[str, Any], status: int) -> None:
        self.body = body
        self.status = status
        self.paths: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(api=self))
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``http://127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)


@contextmanager
def refusing_api(status: int, body: dict[str, Any]) -> Generator[LocalRefusingApi]:
    """A running stand-in that answers every request with ``status`` and ``body``."""
    server = LocalRefusingApi(body=body, status=status)
    server.start()
    try:
        yield server
    finally:
        server.stop()
