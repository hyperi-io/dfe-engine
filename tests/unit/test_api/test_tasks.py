#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_tasks.py
#  Purpose:      Tests for tasks REST API and Phase 3 routers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for Phase 3 operational routers: tasks, hunts, queries, pipeline."""

from __future__ import annotations


class TestTasksRouter:
    """GET/POST /api/v1/tasks endpoints."""

    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/tasks", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/tasks/nonexistent-id", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_cancel_nonexistent_returns_404(self, client, admin_headers):
        resp = client.post("/api/v1/tasks/nonexistent-id/cancel", headers=admin_headers)
        assert resp.status_code == 404

    def test_list_filtered_by_kind_empty(self, client, admin_headers):
        resp = client.get("/api/v1/tasks?kind=hunt:execute", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_stream_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/tasks/nonexistent-id/stream")
        assert resp.status_code == 404


class TestHuntsRouter:
    """GET/POST /api/v1/hunts endpoints."""

    def test_status_no_engine(self, client, admin_headers):
        """Returns running=False when hunt engine is not configured."""
        resp = client.get("/api/v1/hunts/status", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["hunt_count"] == 0

    def test_list_no_engine(self, client, admin_headers):
        """Returns empty list when hunt engine is not configured."""
        resp = client.get("/api/v1/hunts", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_trigger_no_engine_returns_503(self, client, admin_headers):
        """Returns 503 when trying to trigger a hunt without engine running."""
        resp = client.post(
            "/api/v1/hunts/test-hunt/run",
            json={"customer": "test-org"},
            headers=admin_headers,
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_requires_auth(self, client):
        """Endpoints require authentication when auth is enabled."""
        resp = client.get("/api/v1/hunts/status")
        assert resp.status_code == 401


class TestQueriesRouter:
    """GET/POST /api/v1/queries endpoints."""

    def test_views_not_configured_returns_503(self, client, admin_headers):
        """Returns 503 when ViewExecutor is not configured."""
        resp = client.get("/api/v1/queries/views", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_namespaces_not_configured_returns_503(self, client, admin_headers):
        resp = client.get("/api/v1/queries/views/namespaces", headers=admin_headers)
        assert resp.status_code == 503

    def test_get_view_not_configured_returns_503(self, client, admin_headers):
        resp = client.get("/api/v1/queries/views/analytics/user_activity", headers=admin_headers)
        assert resp.status_code == 503

    def test_execute_view_not_configured_returns_503(self, client, admin_headers):
        resp = client.post(
            "/api/v1/queries/views/analytics/test/execute",
            json={"params": {}},
            headers=admin_headers,
        )
        assert resp.status_code == 503

    def test_raw_query_invalid_datasource(self, client, admin_headers):
        resp = client.post(
            "/api/v1/queries/raw",
            json={
                "datasource": "nonexistent:default",
                "query": "test",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_datasource"

    def test_requires_auth_for_views(self, client):
        resp = client.get("/api/v1/queries/views")
        assert resp.status_code == 401


class TestPipelineRouter:
    """GET/POST /api/v1/pipeline endpoints."""

    def test_list_templates(self, client, admin_headers):
        """Returns list (possibly empty) without error."""
        resp = client.get("/api/v1/pipeline/templates", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_build_returns_202(self, client, admin_headers):
        """Build request returns 202 with task_id."""
        resp = client.post(
            "/api/v1/pipeline/build",
            json={"build_core": False},
            headers=admin_headers,
        )
        assert resp.status_code == 202
        data = resp.json()
        assert "task_id" in data
        assert len(data["task_id"]) > 0

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/pipeline/templates")
        assert resp.status_code == 401
