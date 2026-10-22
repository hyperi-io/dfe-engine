#  Project:      dfe-engine
#  File:         tests/support/local_kube_api.py
#  Purpose:      Loopback stand-in for the Kubernetes API server's Secret get and update
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Loopback stand-in for the Kubernetes API server's Secret ``get`` and ``update``.

It answers as the API server does for a service account whose Role grants ``get`` and
``update`` on named Secrets only: 401 for another token, 403 for any other Secret, 404
for a granted Secret that does not exist, and 409 for a stale ``resourceVersion``. The
engine reaches it through scalo's ``kubernetes`` secrets provider, never directly.
"""

import base64
import json
import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from scalo.secrets import SecretsManager

from tests.support.loopback import stop_server

NAMESPACE = "dfe"
SECRET = "dfe-ingest-bearer-tokens"
_SECRET_PATH = re.compile(r"^/api/v1/namespaces/(?P<ns>[^/]+)/secrets/(?P<name>[^/?]+)$")


class LocalKubeApi:
    """The stand-in's Secrets, the Secrets the Role grants, and the requests it answered."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.secrets: dict[tuple[str, str], dict[str, Any]] = {}
        self.granted: set[tuple[str, str]] = {(NAMESPACE, SECRET)}
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._version = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(api=self))
        self._thread = threading.Thread(daemon=True, target=self._server.serve_forever)

    @property
    def base_url(self) -> str:
        """The server origin, such as ``http://127.0.0.1:54321``."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def create_empty(self, name: str = SECRET) -> None:
        """Create the Secret with no data, as the chart does."""
        with self._lock:
            self._version += 1
            self.secrets[(NAMESPACE, name)] = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "resourceVersion": str(self._version),
                },
                "type": "Opaque",
            }

    def data(self, name: str = SECRET) -> dict[str, str]:
        """The Secret's data, decoded."""
        with self._lock:
            stored = self.secrets[(NAMESPACE, name)].get("data") or {}
            return {key: base64.b64decode(value).decode("utf-8") for key, value in stored.items()}

    def puts(self) -> int:
        """How many updates the stand-in received."""
        with self._lock:
            return sum(1 for method, _ in self.requests if method == "PUT")

    def answer(
        self, method: str, path: str, authorization: str, body: bytes
    ) -> tuple[int, dict[str, Any]]:
        """Answer one request as the API server would."""
        with self._lock:
            self.requests.append((method, path))
            if authorization != f"Bearer {self.token}":
                return 401, {"kind": "Status", "code": 401, "message": "Unauthorized"}
            match = _SECRET_PATH.match(path)
            target = (match["ns"], match["name"]) if match else None
            if target is None or target not in self.granted:
                return 403, {"kind": "Status", "code": 403, "message": "forbidden"}
            current = self.secrets.get(target)
            if current is None:
                return 404, {"kind": "Status", "code": 404, "message": "not found"}
            if method == "GET":
                return 200, json.loads(json.dumps(current))
            incoming = json.loads(body)
            if (
                incoming["metadata"].get("resourceVersion")
                != current["metadata"]["resourceVersion"]
            ):
                return 409, {
                    "kind": "Status",
                    "code": 409,
                    "message": "the object has been modified",
                }
            self._version += 1
            incoming["metadata"]["resourceVersion"] = str(self._version)
            self.secrets[target] = incoming
            return 200, json.loads(json.dumps(incoming))

    def start(self) -> None:
        """Serve requests on a background thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the socket."""
        stop_server(self._server, self._thread)


def _handler_for(*, api: LocalKubeApi) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class that answers from ``api``."""

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            status, payload = api.answer(
                self.command, self.path, self.headers.get("Authorization", ""), body
            )
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            """Answer a get."""
            self._reply()

        def do_PUT(self) -> None:
            """Answer an update."""
            self._reply()

        def log_message(self, format: str, *args: object) -> None:
            """Keep request logging out of the test output."""

    return _Handler


@contextmanager
def local_kube_api(token: str) -> Generator[LocalKubeApi]:
    """A running stand-in accepting ``token`` as the service account's."""
    server = LocalKubeApi(token)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def mirror_for(api: LocalKubeApi, token_file: Path, name: str = SECRET) -> SecretsManager:
    """The mirror manager the engine builds, pointed at the stand-in instead of the pod's API server."""
    return SecretsManager.from_config(
        {
            "kubernetes": {
                "secret_name": name,
                "namespace": NAMESPACE,
                "api_server": api.base_url,
                "allow_insecure_http": True,
                "token_path": str(token_file),
                "ca_cert": None,
                "conflict_retries": 1,
            },
            "cache": {"enabled": False},
        }
    )
