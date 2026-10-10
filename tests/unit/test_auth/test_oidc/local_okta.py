#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_okta.py
#  Purpose:      Local HTTP server answering Okta Management API group listings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP server answering Okta Management API group listings."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.support.loopback import stop_server


def _handler_for(*, okta: LocalOkta) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the settings of ``okta``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Answer one page of ``/groups``, linking the next page in a ``Link`` header."""
            split = urlsplit(self.path)
            if split.path != "/groups":
                self.send_error(404)
                return
            index = int(parse_qs(split.query).get("page", ["0"])[0])
            okta.pages_requested.append(index)
            if okta.fail_page == index:
                status, body = okta.fail_status, okta.fail_body
            else:
                status, body = 200, okta.pages[index]
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if status == 200 and index + 1 < len(okta.pages):
                self.send_header("Link", f'<{okta.base_url}/groups?page={index + 1}>; rel="next"')
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalOkta:
    """An Okta Management API stand-in on 127.0.0.1 serving ``/groups`` in pages.

    ``pages`` holds the group objects of each page, and every page but the last
    links the next in a ``Link`` header. ``fail_page`` is the index of a page that
    answers ``fail_status`` and ``fail_body`` instead. ``pages_requested`` records
    the index of each request.
    """

    def __init__(self, *, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.fail_page: int | None = None
        self.fail_status = 500
        self.fail_body: dict[str, Any] = {}
        self.pages_requested: list[int] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(okta=self))
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
