#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/local_idp.py
#  Purpose:      Local HTTP OIDC provider: discovery, JWKS, token and userinfo endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Local HTTP OIDC provider: discovery, JWKS, token and userinfo endpoints."""

import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from joserfc.jwk import RSAKey

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
            """Serve the discovery document, the key set and the userinfo endpoint."""
            if self.path == "/.well-known/openid-configuration" and idp.discovery_body is not None:
                self._reply(body=idp.discovery_body, status=200)
            elif self.path == "/.well-known/openid-configuration":
                self._reply(body=json.dumps(idp.metadata()), status=200)
            elif self.path == "/jwks":
                idp.jwks_fetches += 1
                keys = [key.as_dict(private=False) for key in idp.published_keys]
                self._reply(body=json.dumps({"keys": keys}), status=200)
            elif self.path == "/userinfo":
                self._reply(body=idp.userinfo_body, status=idp.userinfo_status)
            else:
                self._reply(body="{}", status=404)

        def do_POST(self) -> None:
            """Redeem an authorization code once, and only with the verifier of its challenge."""
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            issued = idp.codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            if self.path != "/token" or issued is None or s256(verifier) != issued["challenge"]:
                self._reply(body='{"error": "invalid_grant"}', status=400)
                return
            body = {"access_token": issued["access_token"], "token_type": "Bearer"}
            body["id_token"] = issued["id_token"]
            self._reply(body=json.dumps(body), status=200)

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


def s256(verifier: str) -> str:
    """The PKCE S256 challenge of ``verifier``."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class LocalIdp:
    """An OIDC provider on 127.0.0.1 that each test configures.

    ``discovery_body``, when set, is served with 200 in place of the discovery document.
    ``published_keys`` is the key set ``/jwks`` serves, and ``jwks_fetches`` counts its reads.
    ``codes`` maps an issued authorization code to the challenge it was issued for and
    the token response it redeems for.
    """

    def __init__(self) -> None:
        self.advertise_userinfo = True
        self.discovery_body: str | None = None
        self.userinfo_body = "{}"
        self.userinfo_status = 200
        self.signing_key = RSAKey.generate_key(2048, {"kid": "k1"})
        self.published_keys = [self.signing_key]
        self.jwks_fetches = 0
        self.codes: dict[str, dict[str, Any]] = {}
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

    def metadata(self) -> dict[str, Any]:
        """The discovery document."""
        metadata = {
            "issuer": self.base_url,
            "authorization_endpoint": f"{self.base_url}/authorize",
            "token_endpoint": f"{self.base_url}/token",
            "jwks_uri": f"{self.base_url}/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        if self.advertise_userinfo:
            metadata["userinfo_endpoint"] = f"{self.base_url}/userinfo"
        return metadata

    def issue_code(self, *, challenge: str, id_token: str, access_token: str) -> str:
        """An authorization code bound to ``challenge`` that redeems for the given tokens."""
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "access_token": access_token,
            "challenge": challenge,
            "id_token": id_token,
        }
        return code

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)
