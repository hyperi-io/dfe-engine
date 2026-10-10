#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_graph.py
#  Purpose:      Local HTTP server answering Microsoft Graph transitiveMemberOf listings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP server answering Microsoft Graph transitiveMemberOf listings."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from tests.support.loopback import stop_server

_LISTING = "/transitiveMemberOf/microsoft.graph.group"


def _handler_for(*, graph: LocalGraph) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the settings of ``graph``."""

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
            """Answer one page of a listing for ``/me`` or ``/users/{id}``."""
            split = urlsplit(self.path)
            principal, _, rest = split.path.removeprefix("/").partition(_LISTING)
            if not split.path.endswith(_LISTING) or rest:
                self._reply(body={}, status=404)
                return
            graph.requests.append(
                {"authorization": self.headers.get("Authorization", ""), "principal": principal}
            )
            if principal in graph.refuse:
                status, body = graph.refuse[principal]
                self._reply(body=body, status=status)
                return
            index = int(
                dict(p.split("=", 1) for p in split.query.split("&") if "=" in p).get("page", 0)
            )
            if graph.fail_page.get(principal) == index:
                self._reply(body={"error": {"code": "BadRequest"}}, status=400)
                return
            pages = graph.pages.get(principal) or [[]]
            value = pages[index]
            if principal in graph.limited:
                value = [
                    {
                        "@odata.type": "#microsoft.graph.group",
                        "id": item["id"],
                        "displayName": None,
                        "mail": None,
                        "description": None,
                    }
                    for item in value
                ]
            page: dict[str, Any] = {"value": value}
            if index + 1 < len(pages):
                page["@odata.nextLink"] = f"{graph.base_url}/{principal}{_LISTING}?page={index + 1}"
            self._reply(body=page, status=200)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalGraph:
    """A Microsoft Graph stand-in on 127.0.0.1 whose listings each test sets.

    ``pages`` maps a principal path (``me`` or ``users/<oid>``) to its result
    pages, and every page but the last hands out an ``@odata.nextLink``.
    ``refuse`` maps a principal to the status and Graph error body it answers
    instead. ``limited`` names principals answered the way Graph answers a token
    that may not read the groups: each object with only its id, the rest null.
    ``fail_page`` maps a principal to the page index that answers 400.
    ``requests`` records the principal and Authorization of each call.
    """

    def __init__(self) -> None:
        self.pages: dict[str, list[list[dict[str, Any]]]] = {}
        self.refuse: dict[str, tuple[int, dict[str, Any]]] = {}
        self.limited: set[str] = set()
        self.fail_page: dict[str, int] = {}
        self.requests: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(graph=self))
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``http://127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def principals(self) -> list[str]:
        """The principal path of each request, in order."""
        return [request["principal"] for request in self.requests]

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)
