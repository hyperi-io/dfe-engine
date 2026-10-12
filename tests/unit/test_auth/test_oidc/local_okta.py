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

from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from tests.support.loopback import stop_server


class PlainHttpOkta(OktaAdapter):
    """Okta whose Management API is a plain-http stand-in; the adapter builds only https origins."""

    def __init__(self, provider: OIDCProvider, *, base_url: str) -> None:
        super().__init__(provider)
        self._base_url = base_url

    def _api_base_and_headers(self) -> tuple[str | None, dict[str, str] | None]:
        return self._base_url, {"Accept": "application/json", "Authorization": "SSWS test-token"}


def okta_adapter(base_url: str) -> OktaAdapter:
    """An api-mode Okta adapter whose Management API is at ``base_url``."""
    provider = OIDCProvider(
        type="okta",
        issuer="https://example.okta.com",
        groups=GroupResolutionConfig(mode="api", okta_domain="example.okta.com"),
    )
    return PlainHttpOkta(provider, base_url=base_url)


def _handler_for(*, okta: LocalOkta) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the settings of ``okta``."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Answer one page of ``/groups`` or ``/users/<id>/groups``, linking the next in a ``Link`` header."""
            split = urlsplit(self.path)
            user_listing = split.path.startswith("/users/") and split.path.endswith("/groups")
            if split.path != "/groups" and not user_listing:
                self.send_error(404)
                return
            index = int(parse_qs(split.query).get("page", ["0"])[0])
            okta.pages_requested.append(index)
            body: object
            if okta.fail_page == index:
                status, body = okta.fail_status, okta.fail_body
            elif okta.malformed_page == index:
                status, body = 200, {"errorSummary": "not a page of groups"}
            else:
                status, body = 200, okta.pages[index]
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if status == 200 and index + 1 < len(okta.pages):
                next_page = f"{okta.base_url}{split.path}?page={index + 1}"
                self.send_header("Link", f'<{next_page}>; rel="next"')
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalOkta:
    """An Okta Management API stand-in on 127.0.0.1 serving ``/groups`` and ``/users/<id>/groups`` in pages.

    ``pages`` holds the group objects of each page, and every page but the last
    links the next in a ``Link`` header. ``fail_page`` is the index of a page that
    answers ``fail_status`` and ``fail_body`` instead, and ``malformed_page`` the
    index of one that answers 200 with an object rather than a list of groups.
    ``pages_requested`` records the index of each request.
    """

    def __init__(self, *, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.fail_page: int | None = None
        self.fail_status = 500
        self.fail_body: dict[str, Any] = {}
        self.malformed_page: int | None = None
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
