#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_directory_api.py
#  Purpose:      Local HTTP server answering Google Admin SDK Directory group listings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP server answering Google Admin SDK Directory group listings, and the Google adapter that reads it."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build

from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import OIDCProvider
from tests.support.loopback import stop_server

_LISTING_PATH = "/admin/directory/v1/groups"


def _handler_for(*, directory: LocalDirectoryApi) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the settings of ``directory``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Answer one page of the group listing, naming a next page until ``pages`` are served."""
            split = urlsplit(self.path)
            if split.path != _LISTING_PATH:
                self.send_error(404)
                return
            directory.served += 1
            params = {key: values[0] for key, values in parse_qs(split.query).items()}
            directory.requests.append(params)
            page: dict[str, Any] = {
                "groups": [
                    {"id": f"g{directory.served}", "name": f"group {directory.served}"},
                ]
            }
            if directory.served < directory.pages:
                page["nextPageToken"] = str(directory.served)
            encoded = json.dumps(page).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalDirectoryApi:
    """A Google Admin SDK Directory stand-in on 127.0.0.1 listing one group a page.

    Every page but the ``pages``-th names a next page, so a large ``pages`` is a
    listing that never ends. ``served`` counts the pages answered and ``requests``
    holds each request's query parameters.
    """

    def __init__(self, *, pages: int) -> None:
        self.pages = pages
        self.requests: list[dict[str, str]] = []
        self.served = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(directory=self))
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``http://127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def service(self) -> Any:
        """A googleapiclient Directory API client that reads this server."""
        return directory_service(self.base_url)

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)


def directory_service(base_url: str) -> Any:
    """A googleapiclient Directory API client built against the stand-in at *base_url*."""
    return build(
        "admin",
        "directory_v1",
        cache_discovery=False,
        client_options={"api_endpoint": f"{base_url}/"},
        credentials=AnonymousCredentials(),
    )


class LocalDirectoryGoogle(GoogleAdapter):
    """Google whose Directory API client is built against a local stand-in."""

    def __init__(self, provider: OIDCProvider, *, base_url: str) -> None:
        """Read the Directory API at *base_url*."""
        super().__init__(provider)
        self._service = directory_service(base_url)

    def _get_service(self) -> Any | None:
        return self._service
