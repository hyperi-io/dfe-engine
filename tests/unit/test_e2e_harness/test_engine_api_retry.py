#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_engine_api_retry.py
#  Purpose:      Assert the live suites' engine client rides out an engine that is briefly unreachable
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rollout or a node consolidation takes the engine off the air mid-run, so the client retries the connection."""

import http.server
import socket
import threading
import time

import pytest

from tests.e2e import engine_api
from tests.e2e.engine_api import EngineAPI


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Teapot(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(418)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def _client(port: int) -> EngineAPI:
    return EngineAPI(
        base=f"http://127.0.0.1:{port}", user="admin", password="x", verify=False, token="t"
    )


def test_a_refused_connection_is_retried_until_the_engine_answers() -> None:
    port = _free_port()

    def start_late() -> None:
        time.sleep(1.5)
        late = http.server.HTTPServer(("127.0.0.1", port), _Teapot)
        late.handle_request()
        late.server_close()

    thread = threading.Thread(target=start_late, daemon=True)
    thread.start()
    started = time.monotonic()
    response = _client(port).call("GET", "/health")
    thread.join(timeout=10)

    assert response.status_code == 418
    assert time.monotonic() - started >= 1.5


def test_an_answer_is_returned_whatever_its_status() -> None:
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _Teapot)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    response = _client(port).call("GET", "/health")
    thread.join(timeout=10)
    server.server_close()

    assert response.status_code == 418


def test_an_engine_that_never_answers_fails_with_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_api, "TRANSPORT_RETRY_WINDOW", 0.5)
    port = _free_port()

    with pytest.raises(AssertionError, match=r"never answered GET .* within 0\.5s"):
        _client(port).call("GET", "/health")
