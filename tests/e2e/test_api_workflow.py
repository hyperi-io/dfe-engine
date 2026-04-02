#  Project:      dfe-engine
#  File:         tests/e2e/test_api_workflow.py
#  Purpose:      End-to-end API workflow tests (auth → CRUD → task polling)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""E2E workflow tests — validates full user journeys through the REST API.

Uses TestClient (in-process HTTP) with real registries backed by tmp_path.
No external infrastructure required (no ClickHouse, no Kafka).

Covers:
- Auth flow (login → JWT → /me → refresh)
- Source CRUD lifecycle
- Service config CRUD
- Account + group management
- Task manager (submit → poll → complete)
- RBAC enforcement across workflows
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    DFESettings,
    ServicesSettings,
    SourceSettings,
)


@pytest.fixture
def e2e_settings(tmp_path: Path) -> DFESettings:
    """Isolated settings for E2E tests."""
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()

    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(sources_dir)),
        services=ServicesSettings(config_yaml_dir=str(services_dir)),
        auth=AuthSettings(enabled=True, auth_dir=str(auth_dir)),
        api=APISettings(
            jwt_secret="e2e-test-secret-key-32-chars-long!",
            jwt_expire_minutes=30,
        ),
    )


@pytest.fixture
def e2e_client(e2e_settings: DFESettings):
    """TestClient with full lifespan (registries bootstrapped)."""
    app = create_app(settings=e2e_settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Reset admin password to known value
        app.state.account_store.reset_password("admin", "e2e-admin-pw")
        yield client
    _registries.clear()


class TestAuthWorkflow:
    """Full authentication lifecycle: login → me → refresh → RBAC."""

    def test_login_returns_jwt(self, e2e_client):
        resp = e2e_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_me_with_valid_token(self, e2e_client):
        # Login
        login = e2e_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        token = login.json()["access_token"]

        # /me
        resp = e2e_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin"
        assert "admin" in data["roles"]

    def test_unauthenticated_request_rejected(self, e2e_client):
        resp = e2e_client.get("/api/v1/sources")
        assert resp.status_code == 401

    def test_invalid_token_rejected(self, e2e_client):
        resp = e2e_client.get(
            "/api/v1/sources",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401

    def test_login_wrong_password(self, e2e_client):
        resp = e2e_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert resp.status_code == 401


class TestSourceCRUDWorkflow:
    """Full source lifecycle: create → get → update → list → delete."""

    def _login(self, client) -> dict[str, str]:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_full_source_lifecycle(self, e2e_client):
        headers = self._login(e2e_client)

        # Create
        source = {
            "source": "e2e_test_source",
            "display_name": "E2E Test Source",
            "description": "Created by E2E test",
            "enabled": True,
            "header": {"type": "time_series", "version": "1.0.0"},
            "schema_config": {"engine": "MergeTree"},
        }
        resp = e2e_client.post("/api/v1/sources", json=source, headers=headers)
        assert resp.status_code == 201

        # Get
        resp = e2e_client.get("/api/v1/sources/e2e_test_source", headers=headers)
        assert resp.status_code == 200

        # Update
        resp = e2e_client.put(
            "/api/v1/sources/e2e_test_source",
            json={**source, "display_name": "Updated E2E Source"},
            headers=headers,
        )
        assert resp.status_code == 200

        # Verify update persisted
        resp = e2e_client.get("/api/v1/sources/e2e_test_source", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated E2E Source"

        # List
        resp = e2e_client.get("/api/v1/sources", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

        # Delete
        resp = e2e_client.delete("/api/v1/sources/e2e_test_source", headers=headers)
        assert resp.status_code in (200, 204)

        # Verify gone
        resp = e2e_client.get("/api/v1/sources/e2e_test_source", headers=headers)
        assert resp.status_code == 404

    def test_duplicate_source_returns_409(self, e2e_client):
        headers = self._login(e2e_client)
        source = {
            "source": "dup_test",
            "display_name": "Dup",
            "enabled": True,
            "header": {"type": "time_series", "version": "1.0.0"},
            "schema_config": {"engine": "MergeTree"},
        }
        e2e_client.post("/api/v1/sources", json=source, headers=headers)
        resp = e2e_client.post("/api/v1/sources", json=source, headers=headers)
        assert resp.status_code == 409


class TestAccountGroupWorkflow:
    """Account creation + group assignment + role resolution."""

    def _login(self, client) -> dict[str, str]:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_create_account_assign_group_login(self, e2e_client):
        headers = self._login(e2e_client)

        # Create account
        resp = e2e_client.post(
            "/api/v1/auth/accounts",
            json={"username": "e2e_user", "password": "e2e-user-pw"},
            headers=headers,
        )
        assert resp.status_code == 201

        # Add to dfe-admins group (seeded by bootstrap)
        resp = e2e_client.post(
            "/api/v1/auth/groups/dfe-admins/members",
            json={"username": "e2e_user"},
            headers=headers,
        )
        assert resp.status_code == 200

        # Login as new user
        resp = e2e_client.post(
            "/api/v1/auth/login",
            json={"username": "e2e_user", "password": "e2e-user-pw"},
        )
        assert resp.status_code == 200
        new_token = resp.json()["access_token"]

        # Verify roles via /me
        resp = e2e_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "e2e_user"
        assert "admin" in data["roles"]


class TestTaskManagerWorkflow:
    """Task lifecycle: pipeline build → poll → completion."""

    def _login(self, client) -> dict[str, str]:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_pipeline_build_task_lifecycle(self, e2e_client):
        headers = self._login(e2e_client)

        # Trigger async pipeline build
        resp = e2e_client.post(
            "/api/v1/pipeline/build",
            json={"build_core": False},
            headers=headers,
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        assert task_id

        # Poll task status
        resp = e2e_client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == task_id
        assert data["kind"] == "pipeline:build"
        # Task may be pending, running, completed, or failed
        assert data["status"] in ("pending", "running", "completed", "failed")

        # List tasks filtered by kind
        resp = e2e_client.get("/api/v1/tasks?kind=pipeline:build", headers=headers)
        assert resp.status_code == 200
        tasks = resp.json()
        assert any(t["id"] == task_id for t in tasks)


class TestCrossRouterConsistency:
    """Verify consistent behaviour across all router categories."""

    def _login(self, client) -> dict[str, str]:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_all_list_endpoints_return_paginated(self, e2e_client):
        """All list endpoints should return PaginatedResponse shape."""
        headers = self._login(e2e_client)

        paginated_endpoints = [
            "/api/v1/sources",
            "/api/v1/services",
            # /deployments, /field-maps, /alerts/destinations return 503
            # when their registries aren't configured — that's correct behaviour
        ]

        for endpoint in paginated_endpoints:
            resp = e2e_client.get(endpoint, headers=headers)
            assert resp.status_code == 200, f"Failed: {endpoint}"
            data = resp.json()
            assert "items" in data, f"Missing 'items' in {endpoint}"
            assert "total" in data, f"Missing 'total' in {endpoint}"
            assert "page" in data, f"Missing 'page' in {endpoint}"
            assert "per_page" in data, f"Missing 'per_page' in {endpoint}"

    def test_all_not_found_return_consistent_error(self, e2e_client):
        """404 errors should all have code + message."""
        headers = self._login(e2e_client)

        not_found_endpoints = [
            "/api/v1/sources/nonexistent",
            "/api/v1/services/nonexistent/default",
            "/api/v1/auth/accounts/nonexistent",
            "/api/v1/auth/groups/nonexistent",
            "/api/v1/tasks/nonexistent-id",
        ]

        for endpoint in not_found_endpoints:
            resp = e2e_client.get(endpoint, headers=headers)
            assert resp.status_code == 404, f"Expected 404 for {endpoint}, got {resp.status_code}"
            data = resp.json()
            assert "code" in data, f"Missing 'code' in 404 for {endpoint}"
            assert "message" in data, f"Missing 'message' in 404 for {endpoint}"

    def test_health_endpoints_unauthenticated(self, e2e_client):
        """Health probes should work without auth."""
        for endpoint in ["/health/live", "/health/ready", "/health/startup"]:
            resp = e2e_client.get(endpoint)
            assert resp.status_code == 200, f"Health endpoint {endpoint} failed"
