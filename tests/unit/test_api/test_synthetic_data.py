#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_synthetic_data.py
#  Purpose:      API contract tests for the synthetic data router (packs/generate/stream)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Synthetic data API tests - pack listing, bounded inline generation, stream tasks, RBAC.

Generation runs against the real dfe-schemas package (skipped when absent).
The stream tests post to a closed local port - a real connection failure, no
service and no mocks - and assert degrade-not-die accounting.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dfe_engine.orgs.ingest_credentials import SYNTHETIC_DATA_CREDENTIAL
from dfe_engine.schema.schema_loader import _resolve_package_schemas_root
from dfe_engine.settings import SyntheticDataSettings
from dfe_engine.synthetic_data.service import SyntheticDataService
from tests.support.orgs import name_your_org

_SCHEMAS_ROOT = _resolve_package_schemas_root()
SYSLOG = (_SCHEMAS_ROOT / "meta" / "syslog.yaml") if _SCHEMAS_ROOT else None

needs_packs = pytest.mark.skipif(
    SYSLOG is None or not SYSLOG.exists(),
    reason="dfe-schemas package without reference packs",
)


@pytest.fixture
def receiver() -> Iterator[tuple[str, list[str]]]:
    """A local ingest URL that takes every POST, and the Authorization header each one carried."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            """Keep the request log out of the test output."""

    seen: list[str] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(daemon=True, target=server.serve_forever).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/ingest", seen
    server.shutdown()
    server.server_close()


@needs_packs
class TestPacks:
    def test_lists_reference_packs(self, client, admin_headers):
        r = client.get("/api/v1/synthetic-data/packs", headers=admin_headers)
        assert r.status_code == 200, r.text
        refs = {p["ref"] for p in r.json()["items"]}
        assert {"meta/syslog", "meta/otel/logs", "meta/beats/filebeat"} <= refs
        syslog = next(p for p in r.json()["items"] if p["ref"] == "meta/syslog")
        assert syslog["columns"] > 0
        assert syslog["scenarios"] > 0

    def test_viewer_can_read_packs(self, client, viewer_headers):
        r = client.get("/api/v1/synthetic-data/packs", headers=viewer_headers)
        assert r.status_code == 200, r.text


@needs_packs
class TestGenerate:
    def test_inline_batch(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/generate",
            json={"schema": "meta/syslog", "count": 25, "seed": 42},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema"] == "meta/syslog"
        assert body["count"] == 25
        assert len(body["events"]) == 25
        for event in body["events"]:
            assert event["tags"]["synthetic"] is True
            assert event["message"]

    def test_seed_is_deterministic(self, client, admin_headers):
        # Fixed timestamps come from the batch end - regenerate quickly and
        # compare the non-time fields only.
        payload = {"schema": "meta/syslog", "count": 10, "seed": 7}
        a = client.post(
            "/api/v1/synthetic-data/generate", json=payload, headers=admin_headers
        ).json()
        b = client.post(
            "/api/v1/synthetic-data/generate", json=payload, headers=admin_headers
        ).json()
        strip = lambda e: {k: v for k, v in e.items() if k != "timestamp"}  # noqa: E731
        assert [strip(e) for e in a["events"]] == [strip(e) for e in b["events"]]

    def test_count_ceiling_enforced(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/generate",
            json={"schema": "meta/syslog", "count": 10_001},
            headers=admin_headers,
        )
        assert r.status_code == 400
        assert "bad_synthetic_data_request" in r.text

    def test_unknown_schema_rejected(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/generate",
            json={"schema": "meta/nonexistent", "count": 5},
            headers=admin_headers,
        )
        assert r.status_code == 400
        assert "not found" in r.text

    def test_traversal_ref_rejected(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/generate",
            json={"schema": "../pyproject", "count": 5},
            headers=admin_headers,
        )
        assert r.status_code == 400

    def test_viewer_cannot_generate(self, client, viewer_headers):
        r = client.post(
            "/api/v1/synthetic-data/generate",
            json={"schema": "meta/syslog", "count": 5},
            headers=viewer_headers,
        )
        assert r.status_code == 403

    def test_operator_can_generate(self, client, operator_headers):
        r = client.post(
            "/api/v1/synthetic-data/generate",
            json={"schema": "meta/syslog", "count": 5},
            headers=operator_headers,
        )
        assert r.status_code == 200, r.text


class TestLookalike:
    def test_inline_lookalike(self, client, admin_headers):
        rows = [
            {"user": "real.person", "action": "login" if i % 2 else "logout", "n": i}
            for i in range(30)
        ]
        r = client.post(
            "/api/v1/synthetic-data/lookalike",
            json={"rows": rows, "count": 20, "seed": 5},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema"] == "sample"
        assert len(body["events"]) == 20
        for event in body["events"]:
            assert event["tags"]["synthetic"] is True
            assert event["user"] != "real.person"
            assert event["action"] in ("login", "logout")

    def test_missing_input_rejected(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/lookalike", json={"count": 5}, headers=admin_headers
        )
        assert r.status_code == 400
        assert "sample input" in r.text


@needs_packs
class TestStream:
    def test_stream_degrades_on_unreachable_receiver(self, client, admin_headers):
        # Port 9 (discard) is closed - a real connection failure, no mocks.
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "schema": "meta/syslog",
                "count": 5,
                "rate_eps": 200,
                "receiver_url": "http://127.0.0.1:9/ingest",
                "batch_max": 2,
                "wait": 30,
            },
            headers=admin_headers,
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["done"] is True
        summary = body["response"]
        assert summary["emitted"] == 5
        assert summary["failed"] == 5
        assert summary["sent"] == 0

    @pytest.mark.parametrize(
        ("own", "sent", "expected"),
        [
            ("", {}, ""),
            ("127.0.0.1", {}, ""),
            ("", {"Authorization": "Bearer mine"}, "Bearer mine"),
        ],
        ids=["foreign_receiver", "own_receiver_over_http", "caller_auth_stands"],
    )
    def test_receiver_auth(self, admin_headers, app, client, receiver, own, sent, expected):
        url, seen = receiver
        name_your_org(app=app)
        app.state.synthetic_data = SyntheticDataService(
            SyntheticDataSettings(allowed_receiver_hosts=own)
        )
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "batch_max": 1,
                "count": 2,
                "headers": sent,
                "rate_eps": 200,
                "receiver_url": url,
                "schema": "meta/syslog",
                "wait": 30,
            },
            headers=admin_headers,
        )
        held = app.state.org_registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL)
        assert (r.json()["response"]["sent"], set(seen), held) == (2, {expected}, None)

    @pytest.mark.parametrize(
        ("named", "minted"),
        [(True, True), (False, False)],
        ids=["engine_credential_minted", "no_primary_org"],
    )
    def test_the_own_https_receiver_gets_the_engine_credential(
        self, admin_headers, app, client, named, minted
    ):
        if named:
            name_your_org(app=app)
        app.state.synthetic_data = SyntheticDataService(
            SyntheticDataSettings(allowed_receiver_hosts="127.0.0.1")
        )
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "count": 1,
                "rate_eps": 200,
                "receiver_url": "https://127.0.0.1:9/ingest",
                "schema": "meta/syslog",
                "wait": 30,
            },
            headers=admin_headers,
        )
        held = app.state.org_registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL)
        assert (r.status_code, held is not None) == (202, minted)

    def test_stream_task_is_pollable_and_listed(self, client, admin_headers):
        submit = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "schema": "meta/syslog",
                "count": 3,
                "rate_eps": 200,
                "receiver_url": "http://127.0.0.1:9/ingest",
                "wait": 30,
            },
            headers=admin_headers,
        ).json()
        got = client.get(
            f"/api/v1/synthetic-data/streams/{submit['metadata']['id']}", headers=admin_headers
        )
        assert got.status_code == 200
        assert got.json()["metadata"]["status"] == "completed"
        listed = client.get("/api/v1/synthetic-data/streams", headers=admin_headers)
        assert any(op["name"] == submit["name"] for op in listed.json()["items"])

    @pytest.mark.parametrize(
        ("method", "suffix"), [("GET", ""), ("POST", "/cancel")], ids=["poll", "cancel"]
    )
    def test_an_unknown_stream_is_404(self, client, admin_headers, method, suffix):
        r = client.request(
            method, f"/api/v1/synthetic-data/streams/not-a-task{suffix}", headers=admin_headers
        )
        assert r.status_code == 404

    def test_unbounded_stream_rejected(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={"schema": "meta/syslog", "receiver_url": "http://127.0.0.1:9/"},
            headers=admin_headers,
        )
        assert r.status_code == 400
        assert "bound" in r.text

    def test_non_http_receiver_rejected(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "schema": "meta/syslog",
                "count": 1,
                "receiver_url": "ftp://example.com/",
            },
            headers=admin_headers,
        )
        assert r.status_code == 400

    def test_duration_ceiling_enforced(self, client, admin_headers):
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "schema": "meta/syslog",
                "duration_s": 999_999,
                "receiver_url": "http://127.0.0.1:9/",
            },
            headers=admin_headers,
        )
        assert r.status_code == 400

    def test_concurrent_stream_cap_enforced(self, client, admin_headers):
        # Long low-rate streams stay RUNNING. The default cap is 3.
        started = []
        for _ in range(3):
            r = client.post(
                "/api/v1/synthetic-data/stream",
                json={
                    "schema": "meta/syslog",
                    "duration_s": 60,
                    "rate_eps": 0.5,
                    "receiver_url": "http://127.0.0.1:9/ingest",
                },
                headers=admin_headers,
            )
            assert r.status_code == 202, r.text
            started.append(r.headers["location"])
        try:
            r = client.post(
                "/api/v1/synthetic-data/stream",
                json={
                    "schema": "meta/syslog",
                    "duration_s": 60,
                    "receiver_url": "http://127.0.0.1:9/ingest",
                },
                headers=admin_headers,
            )
            assert r.status_code == 409
            assert "too_many_streams" in r.text
        finally:
            for location in started:
                client.post(f"{location}/cancel", headers=admin_headers)

    def test_viewer_cannot_stream(self, client, viewer_headers):
        r = client.post(
            "/api/v1/synthetic-data/stream",
            json={
                "schema": "meta/syslog",
                "count": 1,
                "receiver_url": "http://127.0.0.1:9/",
            },
            headers=viewer_headers,
        )
        assert r.status_code == 403


@needs_packs
def test_a_submission_past_the_task_cap_is_429(client, admin_headers):
    """The task manager's own cap, not the stream cap, refuses the second stream."""
    from dfe_engine.api.task_manager import TaskManager

    app = client.app
    original = app.state.task_manager
    app.state.task_manager = TaskManager(max_active=1)
    slow = {
        "schema": "meta/syslog",
        "count": 1000,
        "rate_eps": 1,
        "receiver_url": "http://127.0.0.1:9/ingest",
    }
    try:
        first = client.post("/api/v1/synthetic-data/stream", json=slow, headers=admin_headers)
        assert first.status_code == 202, first.text
        second = client.post("/api/v1/synthetic-data/stream", json=slow, headers=admin_headers)
        assert second.status_code == 429, second.text
        assert second.json()["code"] == "too_many_tasks"
        client.post(f"{first.headers['location']}/cancel", headers=admin_headers)
    finally:
        app.state.task_manager = original
