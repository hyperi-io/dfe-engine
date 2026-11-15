#  Project:      dfe-engine
#  File:         tests/support/oci_registry.py
#  Purpose:      A loopback OCI registry serving dfe-stack-manifest artifacts
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A real HTTP server speaking the registry calls the stack resolver makes.

It serves the bearer-token challenge, paged tag lists, artifact manifests and layer blobs
for stacks a test defines, and can damage an artifact or slow every answer.
"""

import hashlib
import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPO_PATH = "org/dfe-stack-manifest"
LOADER = "dfe-loader"
RECEIVER = "dfe-receiver"
UI = "dfe-ui"
PAGE_SIZE = 2


def digest_of(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def image_ref(app: str, label: str) -> str:
    return f"registry.example/org/{app}:{label}@{digest_of(f'{app}-{label}')}"


def manifest_yaml(tag: str, apps: dict[str, str]) -> str:
    """A stack manifest as dfe-infra renders it: apps by name, each pinned by digest."""
    lines = [f'stack_version: "{tag}"', "images:"]
    lines += [f'  {app}: "{image_ref(app, label)}"' for app, label in sorted(apps.items())]
    return "\n".join(lines) + "\n"


def running(apps: dict[str, str]) -> list[str]:
    return [image_ref(app, label) for app, label in apps.items()]


ALL_V1 = {LOADER: "v1", RECEIVER: "v1", UI: "v1"}
ALL_V2 = {LOADER: "v2", RECEIVER: "v2", UI: "v2"}


@dataclass
class FakeRegistry:
    """A loopback OCI registry holding stack-manifest artifacts, counting what it serves."""

    host: str = ""
    stacks: dict[str, dict[str, str]] = field(default_factory=dict)
    damage: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    tags_answer: object = None
    token_requests: int = 0
    requests: int = 0

    @property
    def repository(self) -> str:
        return f"{self.host}/{REPO_PATH}"

    def layer(self, tag: str) -> bytes:
        if self.damage.get(tag) == "not-yaml":
            return b"images: [unclosed\n  - {"
        return manifest_yaml(tag, self.stacks[tag]).encode()

    def manifest(self, tag: str) -> dict:
        blob = self.layer(tag)
        title = "readme.txt" if self.damage.get(tag) == "no-layer" else f"dfe-manifest-{tag}.yaml"
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        return {
            "schemaVersion": 2,
            "layers": [
                {
                    "digest": digest,
                    "size": len(blob),
                    "annotations": {"org.opencontainers.image.title": title},
                }
            ],
        }

    def blob(self, digest: str) -> bytes | None:
        for tag in self.stacks:
            real = self.layer(tag)
            if "sha256:" + hashlib.sha256(real).hexdigest() == digest:
                return b"tampered: true\n" if self.damage.get(tag) == "tampered" else real
        return None


def _handler(registry: FakeRegistry) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            registry.requests += 1
            time.sleep(registry.delay_seconds)
            parts = urlsplit(self.path)
            if parts.path == "/token":
                registry.token_requests += 1
                self._send(200, json.dumps({"token": "anonymous"}).encode())
                return
            if self.headers.get("Authorization") != "Bearer anonymous":
                challenge = (
                    f'Bearer realm="http://{registry.host}/token",service="test",'
                    f'scope="repository:{REPO_PATH}:pull"'
                )
                self._send(401, b"{}", {"WWW-Authenticate": challenge})
                return
            prefix = f"/v2/{REPO_PATH}/"
            if not parts.path.startswith(prefix):
                self._send(404, b"{}")
                return
            tail = parts.path.removeprefix(prefix)
            if tail == "tags/list":
                self._tags(parse_qs(parts.query))
            elif (
                tail.startswith("manifests/") and tail.removeprefix("manifests/") in registry.stacks
            ):
                tag = tail.removeprefix("manifests/")
                answer = (
                    [1, 2]
                    if registry.damage.get(tag) == "manifest-array"
                    else registry.manifest(tag)
                )
                body = json.dumps(answer)
                self._send(200, body.encode(), {"Content-Type": "application/json"})
            elif tail.startswith("blobs/") and registry.blob(tail.removeprefix("blobs/")):
                self._send(200, registry.blob(tail.removeprefix("blobs/")) or b"")
            else:
                self._send(404, b"{}")

        def _tags(self, query: dict[str, list[str]]) -> None:
            if registry.tags_answer is not None:
                self._send(200, json.dumps(registry.tags_answer).encode())
                return
            tags = sorted(registry.stacks)
            last = query.get("last", [""])[0]
            remaining = [tag for tag in tags if tag > last] if last else tags
            page, more = remaining[:PAGE_SIZE], len(remaining) > PAGE_SIZE
            headers = {}
            if more:
                headers["Link"] = f'</v2/{REPO_PATH}/tags/list?last={page[-1]}&n=1000>; rel="next"'
            self._send(200, json.dumps({"name": REPO_PATH, "tags": page}).encode(), headers)

    return Handler


@contextmanager
def serve_registry() -> Generator[FakeRegistry]:
    """Run a registry on a loopback port for the length of the block."""
    fake = FakeRegistry()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.host = f"127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
