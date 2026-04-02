#  Project:      dfe-engine
#  File:         tests/e2e/test_org_lifecycle.py
#  Purpose:      End-to-end org lifecycle tests (create, configure, toggle, delete)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""E2E org lifecycle tests — full create → configure → toggle → delete journeys.

Uses TestClient (in-process HTTP) with real registries backed by tmp_path.
No external infrastructure required (no ClickHouse, no HyperDX).
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
    """Isolated settings for E2E org lifecycle tests."""
    (tmp_path / "sources").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "auth").mkdir()

    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
        api=APISettings(
            jwt_secret="org-e2e-test-secret-key-32chars!",
            jwt_expire_minutes=30,
        ),
    )


@pytest.fixture
def e2e_client(e2e_settings: DFESettings):
    """TestClient with full lifespan (registries bootstrapped)."""
    app = create_app(settings=e2e_settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.account_store.reset_password("admin", "e2e-admin-pw")
        yield client
    _registries.clear()


class TestOrgLifecycleE2E:
    """Full org lifecycle: create → configure → toggle dedicated DB → delete."""

    def _login(self, client: TestClient) -> dict[str, str]:
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "e2e-admin-pw"},
        )
        assert resp.status_code == 200, f"Login failed: {resp.text}"
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def test_create_org_default_shared_db(self, e2e_client: TestClient):
        """Create org with default settings (shared DB, no dedicated)."""
        headers = self._login(e2e_client)
        resp = e2e_client.post(
            "/api/v1/orgs",
            json={
                "name": "lifecycle-test",
                "org_ids": ["lt"],
                "display_name": "Lifecycle Test Org",
            },
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "lifecycle-test"
        assert data["display_name"] == "Lifecycle Test Org"
        assert data["org_ids"] == ["lt"]
        assert data["dedicated_database"] is False
        assert data["enabled"] is True

    def test_create_org_with_dedicated_db(self, e2e_client: TestClient):
        """Create org requesting dedicated database."""
        headers = self._login(e2e_client)
        resp = e2e_client.post(
            "/api/v1/orgs",
            json={
                "name": "dedicated-test",
                "org_ids": ["dt"],
                "dedicated_database": True,
            },
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["dedicated_database"] is True

    def test_create_org_minimal(self, e2e_client: TestClient):
        """Create org with only required name field."""
        headers = self._login(e2e_client)
        resp = e2e_client.post(
            "/api/v1/orgs",
            json={"name": "minimal-org", "org_ids": []},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "minimal-org"
        assert data["org_ids"] == []
        assert data["dedicated_database"] is False

    def test_get_org_after_create(self, e2e_client: TestClient):
        """Get a specific org after creating it."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "get-test", "org_ids": ["gt"], "display_name": "Get Test"},
            headers=headers,
        )
        resp = e2e_client.get("/api/v1/orgs/get-test", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "get-test"
        assert resp.json()["display_name"] == "Get Test"

    def test_list_orgs_includes_created(self, e2e_client: TestClient):
        """List returns orgs created via POST."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "list-org-a", "org_ids": ["la"]},
            headers=headers,
        )
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "list-org-b", "org_ids": ["lb"]},
            headers=headers,
        )
        resp = e2e_client.get("/api/v1/orgs", headers=headers)
        assert resp.status_code == 200
        names = [o["name"] for o in resp.json()]
        assert "list-org-a" in names
        assert "list-org-b" in names

    def test_get_org_not_found(self, e2e_client: TestClient):
        """404 with correct error shape for nonexistent org."""
        headers = self._login(e2e_client)
        resp = e2e_client.get("/api/v1/orgs/nonexistent-org", headers=headers)
        assert resp.status_code == 404
        data = resp.json()
        assert "code" in data
        assert "message" in data

    def test_create_duplicate_org_returns_409(self, e2e_client: TestClient):
        """Creating the same org name twice returns 409."""
        headers = self._login(e2e_client)
        payload = {"name": "dup-org", "org_ids": ["dup"]}
        e2e_client.post("/api/v1/orgs", json=payload, headers=headers)
        resp = e2e_client.post("/api/v1/orgs", json=payload, headers=headers)
        assert resp.status_code == 409

    def test_update_org_display_name(self, e2e_client: TestClient):
        """PUT updates display_name."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "upd-test", "org_ids": ["upd"], "display_name": "Original"},
            headers=headers,
        )
        resp = e2e_client.put(
            "/api/v1/orgs/upd-test",
            json={"display_name": "Updated"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated"

    def test_toggle_dedicated_db_off_requires_confirm(self, e2e_client: TestClient):
        """Disabling dedicated DB without confirm_merge returns 400."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "toggle-test", "org_ids": ["tt"], "dedicated_database": True},
            headers=headers,
        )
        resp = e2e_client.put(
            "/api/v1/orgs/toggle-test",
            json={"dedicated_database": False},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "confirmation_required"

    def test_toggle_dedicated_db_off_with_confirm(self, e2e_client: TestClient):
        """Disabling dedicated DB with confirm_merge=True succeeds."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "confirm-test", "org_ids": ["ct"], "dedicated_database": True},
            headers=headers,
        )
        resp = e2e_client.put(
            "/api/v1/orgs/confirm-test",
            json={"dedicated_database": False, "confirm_merge": True},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["dedicated_database"] is False

    def test_toggle_dedicated_db_on(self, e2e_client: TestClient):
        """Enabling dedicated DB on an existing shared-DB org."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "enable-ded", "org_ids": ["ed"]},
            headers=headers,
        )
        resp = e2e_client.put(
            "/api/v1/orgs/enable-ded",
            json={"dedicated_database": True},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["dedicated_database"] is True

    def test_update_org_not_found_returns_404(self, e2e_client: TestClient):
        """PUT on nonexistent org returns 404."""
        headers = self._login(e2e_client)
        resp = e2e_client.put(
            "/api/v1/orgs/no-such-org",
            json={"display_name": "X"},
            headers=headers,
        )
        assert resp.status_code == 404

    def test_delete_org(self, e2e_client: TestClient):
        """Delete org and verify it's gone."""
        headers = self._login(e2e_client)
        e2e_client.post(
            "/api/v1/orgs",
            json={"name": "delete-test", "org_ids": ["delt"]},
            headers=headers,
        )
        resp = e2e_client.delete("/api/v1/orgs/delete-test", headers=headers)
        assert resp.status_code in (200, 204)

        resp = e2e_client.get("/api/v1/orgs/delete-test", headers=headers)
        assert resp.status_code == 404

    def test_delete_org_not_found_returns_404(self, e2e_client: TestClient):
        """DELETE on nonexistent org returns 404."""
        headers = self._login(e2e_client)
        resp = e2e_client.delete("/api/v1/orgs/ghost-org", headers=headers)
        assert resp.status_code == 404

    def test_org_crud_requires_auth(self, e2e_client: TestClient):
        """All org write endpoints require authentication."""
        resp = e2e_client.post(
            "/api/v1/orgs",
            json={"name": "noauth", "org_ids": ["n"]},
        )
        assert resp.status_code == 401

        resp = e2e_client.get("/api/v1/orgs")
        assert resp.status_code == 401

        resp = e2e_client.put("/api/v1/orgs/anything", json={})
        assert resp.status_code == 401

        resp = e2e_client.delete("/api/v1/orgs/anything")
        assert resp.status_code == 401

    def test_full_org_lifecycle(self, e2e_client: TestClient):
        """Complete create → read → update → delete round-trip."""
        headers = self._login(e2e_client)

        # Create
        resp = e2e_client.post(
            "/api/v1/orgs",
            json={
                "name": "full-lifecycle",
                "org_ids": ["fl", "fl-sub"],
                "display_name": "Full Lifecycle Org",
            },
            headers=headers,
        )
        assert resp.status_code == 201
        created = resp.json()
        assert created["name"] == "full-lifecycle"

        # Read
        resp = e2e_client.get("/api/v1/orgs/full-lifecycle", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["org_ids"] == ["fl", "fl-sub"]

        # Update
        resp = e2e_client.put(
            "/api/v1/orgs/full-lifecycle",
            json={"display_name": "Renamed Org", "org_ids": ["fl", "fl-sub", "fl-extra"]},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Renamed Org"

        # Verify update persisted
        resp = e2e_client.get("/api/v1/orgs/full-lifecycle", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Renamed Org"
        assert "fl-extra" in resp.json()["org_ids"]

        # Delete
        resp = e2e_client.delete("/api/v1/orgs/full-lifecycle", headers=headers)
        assert resp.status_code in (200, 204)

        # Verify gone
        resp = e2e_client.get("/api/v1/orgs/full-lifecycle", headers=headers)
        assert resp.status_code == 404
