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

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dfe_engine.clickhouse.cloud import CloudService, CloudServiceError
from dfe_engine.settings import ClickHouseCloudSettings
from tests.support.loopback import stop_server


class _Handler(BaseHTTPRequestHandler):
    """Minimal emulation of the CH Cloud management API, routed by path suffix.

    Suffix routing keeps the test robust to how the client joins ``api_base`` and
    the request path (e.g. whether the ``/v1`` prefix survives).
    """

    def log_message(self, format: str, *args: object) -> None:
        return

    @property
    def api(self) -> _CloudApi:
        """The server this request reached, with the service it emulates."""
        assert isinstance(self.server, _CloudApi)
        return self.server

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, body: dict | None = None) -> None:
        self.api.requests.append(
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
                            "name": self.api.svc_name,
                            "state": self.api.state,
                            **self.api.sizing,
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
            self.api.state = "running"
        elif command == "stop":
            self.api.state = "stopped"
        self._send({"result": {"state": self.api.state}})


class _CloudApi(HTTPServer):
    """The stub server: the one service it emulates, and every request it answered."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = "stopped"
        self.svc_name = "dfe"
        self.sizing: dict[str, object] = {}
        self.requests: list[dict[str, object]] = []


@pytest.fixture
def stub():
    """A real ephemeral HTTP server emulating the CH Cloud management API."""
    server = _CloudApi()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        stop_server(server, thread)


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


class TestServiceSizing:
    """The service object already carries its sizing, so the status read keeps it."""

    def test_the_stated_sizing_rides_the_status(self, stub):
        stub.sizing = {
            "minReplicaMemoryGb": 16,
            "maxReplicaMemoryGb": 120.5,
            "numReplicas": 3,
            "idleScaling": True,
        }

        st = CloudService(_cfg(stub)).status()

        assert (st.min_replica_memory_gb, st.max_replica_memory_gb) == (16.0, 120.5)
        assert st.num_replicas == 3
        assert st.idle_scaling is True

    def test_a_service_object_without_sizing_reads_as_unknown(self, stub):
        st = CloudService(_cfg(stub)).status()

        assert st.min_replica_memory_gb is None
        assert st.max_replica_memory_gb is None
        assert st.num_replicas is None
        assert st.idle_scaling is None

    @pytest.mark.parametrize(
        "sizing",
        [
            {"minReplicaMemoryGb": "16", "numReplicas": 3.0, "idleScaling": "yes"},
            {"maxReplicaMemoryGb": True, "numReplicas": False, "idleScaling": 1},
        ],
    )
    def test_a_field_of_the_wrong_type_is_unknown_rather_than_coerced(self, stub, sizing):
        stub.sizing = sizing

        st = CloudService(_cfg(stub)).status()

        assert st.min_replica_memory_gb is None
        assert st.max_replica_memory_gb is None
        assert st.num_replicas is None
        assert st.idle_scaling is None

    def test_the_observed_read_returns_the_service_object(self, stub):
        from dfe_engine.backing.observed import cloud_service

        stub.sizing = {"numReplicas": 2}

        found = cloud_service(_cfg(stub), timeout=5.0)

        assert found is not None
        assert found.num_replicas == 2

    def test_the_observed_read_of_a_missing_service_is_unknown(self, stub):
        from dfe_engine.backing.observed import cloud_service

        stub.svc_name = "someone-elses-service"

        assert cloud_service(_cfg(stub), timeout=5.0) is None


class TestConfigured:
    def test_unconfigured_default_factory_raises(self):
        # No creds -> the real default factory refuses (never a silent no-op),
        # and never touches the network.
        with pytest.raises(CloudServiceError, match="not configured"):
            CloudService(ClickHouseCloudSettings()).status()
