#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_synthetic_data.py
#  Purpose:      API contract tests for the synthetic data router (packs/generate/stream)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Synthetic data API tests - pack listing, bounded inline generation, stream tasks, RBAC.

Generation runs against the real dfe-schemas submodule (skipped when absent).
The stream tests post to a closed local port - a real connection failure, no
service and no mocks - and assert degrade-not-die accounting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SYSLOG = REPO_ROOT / "schemas" / "meta" / "syslog.yaml"

needs_packs = pytest.mark.skipif(
    not SYSLOG.exists(), reason="dfe-schemas submodule without reference packs"
)


@needs_packs
class TestPacks:
    def test_lists_reference_packs(self, client, admin_headers):
        r = client.get("/api/v1/synthetic-data/packs", headers=admin_headers)
        assert r.status_code == 200, r.text
        refs = {p["ref"] for p in r.json()}
        assert {"meta/syslog", "meta/otel/logs", "meta/beats/filebeat"} <= refs
        syslog = next(p for p in r.json() if p["ref"] == "meta/syslog")
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
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "completed"
        summary = body["result"]
        assert summary["emitted"] == 5
        assert summary["failed"] == 5
        assert summary["sent"] == 0

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
            f"/api/v1/synthetic-data/streams/{submit['task_id']}", headers=admin_headers
        )
        assert got.status_code == 200
        assert got.json()["status"] == "completed"
        listed = client.get("/api/v1/synthetic-data/streams", headers=admin_headers)
        assert any(t["id"] == submit["task_id"] for t in listed.json())

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

    def test_unknown_task_404(self, client, admin_headers):
        r = client.get("/api/v1/synthetic-data/streams/not-a-task", headers=admin_headers)
        assert r.status_code == 404

    def test_cancel_unknown_task_404(self, client, admin_headers):
        r = client.delete("/api/v1/synthetic-data/streams/not-a-task", headers=admin_headers)
        assert r.status_code == 404

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
