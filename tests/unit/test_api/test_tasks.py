#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_tasks.py
#  Purpose:      Tests for tasks REST API and Phase 3 routers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for Phase 3 operational routers: tasks, hunts, queries, pipeline."""

from __future__ import annotations

import time


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
        resp = client.get("/api/v1/tasks/nonexistent-id/stream", headers=admin_headers)
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

    def test_list_empty_registry(self, client, admin_headers):
        """Returns empty paginated list when no hunt configs exist."""
        resp = client.get("/api/v1/hunts", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_search_and_pagination(self, client, admin_headers):
        payload_a = {
            "hunt_id": "alpha_hunt",
            "name": "Alpha Windows Hunt",
            "cron": "* * * * *",
            "global_target_table_name": "logs_alerts",
            "customers": ["org_a"],
            "rules": [{"rule_name": "alpha_rule"}],
        }
        payload_b = {
            "hunt_id": "beta_hunt",
            "name": "Beta Linux Hunt",
            "cron": "* * * * *",
            "global_target_table_name": "logs_alerts",
            "customers": ["org_b"],
            "rules": [{"rule_name": "beta_rule"}],
        }
        assert (
            client.post("/api/v1/hunts", json=payload_a, headers=admin_headers).status_code == 201
        )
        assert (
            client.post("/api/v1/hunts", json=payload_b, headers=admin_headers).status_code == 201
        )

        search = client.get(
            "/api/v1/hunts",
            params={"search": "windows"},
            headers=admin_headers,
        )
        assert search.status_code == 200
        assert search.json()["total"] == 1
        assert search.json()["items"][0]["hunt_id"] == "alpha_hunt"

        page = client.get(
            "/api/v1/hunts",
            params={"page": 1, "per_page": 1, "sort_by": "hunt_id", "sort_order": "asc"},
            headers=admin_headers,
        )
        assert page.status_code == 200
        body = page.json()
        assert body["total"] == 2
        assert len(body["items"]) == 1
        assert body["items"][0]["hunt_id"] == "alpha_hunt"
        assert body["next_page"] == 2

        client.delete("/api/v1/hunts/alpha_hunt", headers=admin_headers)
        client.delete("/api/v1/hunts/beta_hunt", headers=admin_headers)

    def test_create_get_update_delete_hunt(self, client, admin_headers):
        payload = {
            "hunt_id": "api_test_hunt",
            "name": "API Test Hunt",
            "cron": "* * * * *",
            "global_target_table_name": "logs_alerts",
            "global_source_table_name": "logs_nxlog",
            "customers": ["test-org"],
            "rules": [{"rule_name": "win_account_a_common_activities"}],
        }
        create = client.post("/api/v1/hunts", json=payload, headers=admin_headers)
        assert create.status_code == 201
        body = create.json()
        assert body["hunt_id"] == "api_test_hunt"
        assert body["name"] == "API Test Hunt"

        detail = client.get("/api/v1/hunts/api_test_hunt", headers=admin_headers)
        assert detail.status_code == 200
        assert detail.json()["rules"][0]["rule_name"] == "win_account_a_common_activities"

        listed = client.get("/api/v1/hunts", headers=admin_headers)
        assert listed.status_code == 200
        ids = [h["hunt_id"] for h in listed.json()["items"]]
        assert "api_test_hunt" in ids

        update_payload = {
            "name": "API Test Hunt Updated",
            "cron": "*/5 * * * *",
            "global_target_table_name": "logs_alerts",
            "customers": ["test-org"],
            "rules": [{"rule_name": "other_rule"}],
        }
        updated = client.put(
            "/api/v1/hunts/api_test_hunt",
            json=update_payload,
            headers=admin_headers,
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "API Test Hunt Updated"

        deleted = client.delete("/api/v1/hunts/api_test_hunt", headers=admin_headers)
        assert deleted.status_code == 204

        missing = client.get("/api/v1/hunts/api_test_hunt", headers=admin_headers)
        assert missing.status_code == 404

    def test_create_duplicate_returns_409(self, client, admin_headers):
        payload = {
            "hunt_id": "dup_hunt",
            "name": "Dup",
            "cron": "* * * * *",
            "global_target_table_name": "t",
            "customers": ["c"],
            "rules": [{"rule_name": "r"}],
        }
        assert client.post("/api/v1/hunts", json=payload, headers=admin_headers).status_code == 201
        dup = client.post("/api/v1/hunts", json=payload, headers=admin_headers)
        assert dup.status_code == 409
        client.delete("/api/v1/hunts/dup_hunt", headers=admin_headers)

    def test_create_invalid_hunt_id_returns_422(self, client, admin_headers):
        payload = {
            "hunt_id": "Bad-Id",
            "name": "Bad",
            "cron": "* * * * *",
            "global_target_table_name": "t",
            "customers": ["c"],
            "rules": [{"rule_name": "r"}],
        }
        resp = client.post("/api/v1/hunts", json=payload, headers=admin_headers)
        assert resp.status_code == 422

    def test_viewer_cannot_create_hunt(self, client, viewer_headers):
        payload = {
            "hunt_id": "viewer_hunt",
            "name": "Viewer",
            "cron": "* * * * *",
            "global_target_table_name": "t",
            "customers": ["c"],
            "rules": [{"rule_name": "r"}],
        }
        resp = client.post("/api/v1/hunts", json=payload, headers=viewer_headers)
        assert resp.status_code == 403

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

        task_id = data["task_id"]
        detail = None
        for _ in range(20):
            detail = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers)
            if detail.status_code == 200:
                break
            time.sleep(0.05)
        assert detail is not None and detail.status_code == 200, (
            detail.text if detail else "no response"
        )
        assert detail.json()["kind"] == "pipeline:build"

    def test_requires_auth(self, client):
        resp = client.get("/api/v1/pipeline/templates")
        assert resp.status_code == 401
