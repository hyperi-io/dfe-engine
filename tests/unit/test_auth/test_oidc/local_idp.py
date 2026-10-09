#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_idp.py
#  Purpose:      Local HTTP server answering OIDC discovery and userinfo requests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP server answering OIDC discovery and userinfo requests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.support.loopback import stop_server


def _handler_for(*, idp: LocalIdp) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from the settings of ``idp``."""

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self, *, body: str, status: int) -> None:
            """Send ``body`` as a JSON-typed response with ``status``."""
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            """Serve the discovery document and the userinfo endpoint."""
            if self.path == "/.well-known/openid-configuration":
                metadata = {"issuer": idp.base_url, "token_endpoint": f"{idp.base_url}/token"}
                if idp.advertise_userinfo:
                    metadata["userinfo_endpoint"] = f"{idp.base_url}/userinfo"
                self._reply(body=json.dumps(metadata), status=200)
            elif self.path == "/userinfo":
                self._reply(body=idp.userinfo_body, status=idp.userinfo_status)
            else:
                self._reply(body="{}", status=404)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


class LocalIdp:
    """An OIDC provider on 127.0.0.1 whose userinfo response each test configures."""

    def __init__(self) -> None:
        self.advertise_userinfo = True
        self.userinfo_body = "{}"
        self.userinfo_status = 200
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(idp=self))
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``http://127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def metadata_url(self) -> str:
        """The discovery document URL."""
        return f"{self.base_url}/.well-known/openid-configuration"

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)
