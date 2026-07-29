#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_cloud.py
#  Purpose:      CloudService tests against a REAL local HTTP stub (no mocks)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CloudService tests against a REAL local HTTP stub - no mocks, real HttpClient.

Stands up an ephemeral HTTP server that emulates the ClickHouse Cloud management
API and points CloudService's own scalo :class:`HttpClient` at it, so the whole
path is exercised over a real socket: HTTP basic-auth for the control-plane key,
the GET (status) and PATCH (start/stop) round-trips, JSON parsing and the state
transitions. Nothing internal is mocked (the no-mocks standard); the only
double is the external management API, and that is a genuine HTTP server here,
not a stubbed object. A fake clock keeps ``wait_running`` instant.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError
from dfe_engine.settings import ClickHouseCloudSettings


class _Handler(BaseHTTPRequestHandler):
    """Minimal emulation of the CH Cloud management API, routed by path suffix.

    Suffix routing keeps the test robust to how the client joins ``api_base`` and
    the request path (e.g. whether the ``/v1`` prefix survives).
    """

    def log_message(self, *args: object) -> None:  # keep the test output quiet
        return

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, body: dict | None = None) -> None:
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": method,
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )

    def do_GET(self) -> None:  # BaseHTTPRequestHandler dispatch method name
        self._record("GET")
        path = self.path.rstrip("/")
        if path.endswith("/organizations"):
            self._send({"result": [{"id": "org-1"}]})
        elif path.endswith("/services"):
            self._send(
                {
                    "result": [
                        {
                            "id": "svc-1",
                            "name": self.server.svc_name,  # type: ignore[attr-defined]
                            "state": self.server.state,  # type: ignore[attr-defined]
                        }
                    ]
                }
            )
        else:
            self._send({"error": "not found"}, code=404)

    def do_PATCH(self) -> None:  # BaseHTTPRequestHandler dispatch method name
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw or b"{}")
        self._record("PATCH", body)
        command = body.get("command")
        if command == "start":
            self.server.state = "running"  # type: ignore[attr-defined]
        elif command == "stop":
            self.server.state = "stopped"  # type: ignore[attr-defined]
        self._send({"result": {"state": self.server.state}})  # type: ignore[attr-defined]


@pytest.fixture
def stub():
    """A real ephemeral HTTP server emulating the CH Cloud management API."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.state = "stopped"  # type: ignore[attr-defined]
    server.svc_name = "dfe"  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeClock:
    """Injectable clock so ``wait_running`` never really sleeps or races a wall."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _cfg(server, **kw) -> ClickHouseCloudSettings:
    host, port = server.server_address
    base = {
        "api_key_id": "k",
        "api_key_secret": "s",
        "api_base": f"http://{host}:{port}/v1",
        "service_name": "dfe",
    }
    base.update(kw)
    return ClickHouseCloudSettings(**base)


class TestStatus:
    def test_status_reads_state_over_real_http(self, stub):
        st = CloudService(_cfg(stub)).status()
        assert st.id == "svc-1"
        assert st.name == "dfe"
        assert st.state == "stopped"
        assert st.is_stopped
        assert not st.is_running
        # Prove the real HttpClient sent HTTP basic auth (the control-plane key),
        # i.e. `auth=(id, secret)` reached httpx - not a mocked transport.
        expected = "Basic " + base64.b64encode(b"k:s").decode()
        assert any(r["auth"] == expected for r in stub.requests)

    def test_status_resolves_by_service_id(self, stub):
        st = CloudService(_cfg(stub, service_id="svc-1", service_name="")).status()
        assert st.id == "svc-1"

    def test_service_not_found_raises(self, stub):
        stub.svc_name = "someone-elses-service"
        with pytest.raises(CloudServiceError, match="not found"):
            CloudService(_cfg(stub)).status()


class TestStartStop:
    def test_start_then_stop_lifecycle(self, stub):
        svc = CloudService(_cfg(stub))
        assert svc.status().state == "stopped"

        started = svc.start()
        assert started.is_running
        patches = [r for r in stub.requests if r["method"] == "PATCH"]
        assert patches, "start() must issue a PATCH to the /state endpoint"
        assert patches[-1]["body"] == {"command": "start"}
        assert patches[-1]["path"].endswith("/organizations/org-1/services/svc-1/state")

        stopped = svc.stop()
        assert stopped.state == "stopped"
        assert [r for r in stub.requests if r["method"] == "PATCH"][-1]["body"] == {
            "command": "stop"
        }

    def test_start_when_running_is_noop(self, stub):
        stub.state = "running"
        svc = CloudService(_cfg(stub))
        svc.start()
        # Already running -> no billable start issued.
        assert not [r for r in stub.requests if r["method"] == "PATCH"]

    def test_stop_when_stopped_is_noop(self, stub):
        svc = CloudService(_cfg(stub))  # server starts "stopped"
        svc.stop()
        assert not [r for r in stub.requests if r["method"] == "PATCH"]


class TestWaitRunning:
    def test_returns_immediately_when_running(self, stub):
        stub.state = "running"
        clock = _FakeClock()
        svc = CloudService(_cfg(stub), sleep=clock.sleep, now=clock.now)
        assert svc.wait_running(timeout=30.0, poll_interval=10.0).is_running

    def test_times_out_when_never_running(self, stub):
        stub.state = "starting"  # never flips to running
        clock = _FakeClock()
        svc = CloudService(_cfg(stub), sleep=clock.sleep, now=clock.now)
        with pytest.raises(CloudServiceError, match="not running"):
            svc.wait_running(timeout=30.0, poll_interval=10.0)


class TestConfigured:
    def test_unconfigured_default_factory_raises(self):
        # No creds -> the real default factory refuses (never a silent no-op),
        # and never touches the network.
        with pytest.raises(CloudServiceError, match="not configured"):
            CloudService(ClickHouseCloudSettings()).status()
