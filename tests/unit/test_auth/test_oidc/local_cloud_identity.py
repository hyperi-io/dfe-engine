#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_cloud_identity.py
#  Purpose:      Local HTTP server answering Cloud Identity group membership searches
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP server answering Cloud Identity group membership searches."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.support.loopback import stop_server

_SEARCH_PATH = "/groups/-/memberships:"


def _handler_for(*, directory: LocalCloudIdentity) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the settings of ``directory``."""

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self, *, body: dict[str, Any], status: int) -> None:
            """Send ``body`` as JSON with ``status``."""
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            """Answer one page of a membership search, or the refusal set for its method."""
            split = urlsplit(self.path)
            if not split.path.startswith(_SEARCH_PATH):
                self._reply(body={}, status=404)
                return
            method = split.path.removeprefix(_SEARCH_PATH)
            params = {key: values[0] for key, values in parse_qs(split.query).items()}
            directory.requests.append(
                {
                    "authorization": self.headers.get("Authorization", ""),
                    "method": method,
                    "params": params,
                }
            )
            if method in directory.refuse:
                status, body = directory.refuse[method]
                self._reply(body=body, status=status)
                return
            pages = directory.pages.get(method) or [[]]
            index = int(params.get("pageToken") or 0)
            page: dict[str, Any] = {"memberships": pages[index]}
            if index + 1 < len(pages):
                page["nextPageToken"] = str(index + 1)
            self._reply(body=page, status=200)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalCloudIdentity:
    """A Cloud Identity stand-in on 127.0.0.1 whose searches each test sets.

    ``pages`` maps a search method to its result pages, and every page but the
    last hands out a page token. ``refuse`` maps a method to the status and
    Google error body it answers instead. ``requests`` records each search.
    """

    def __init__(self) -> None:
        self.pages: dict[str, list[list[dict[str, Any]]]] = {}
        self.refuse: dict[str, tuple[int, dict[str, Any]]] = {}
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(directory=self))
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``http://127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def methods(self) -> list[str]:
        """The search method of each request, in order."""
        return [request["method"] for request in self.requests]

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)
