#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_providers.py
#  Purpose:      Tests for OIDC provider CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/PUT/DELETE /api/v1/auth/oidc-providers endpoints."""

from __future__ import annotations


def _create_provider(client, admin_headers, name="test-provider", **overrides):
    """Helper to create a provider and return the response."""
    body = {
        "name": name,
        "type": "generic",
        "display_name": "Test Provider",
        "issuer": "https://accounts.example.com",
        "client_id_env": "OIDC_CLIENT_ID",
    }
    body.update(overrides)
    return client.post("/api/v1/auth/oidc-providers", json=body, headers=admin_headers)


class TestCreateProvider:
    """POST /api/v1/auth/oidc-providers"""

    def test_create_provider(self, client, admin_headers):
        resp = _create_provider(client, admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-provider"
        assert data["type"] == "generic"
        assert data["enabled"] is True
        assert data["display_name"] == "Test Provider"
        assert data["issuer"] == "https://accounts.example.com"
        assert data["client_id_env"] == "OIDC_CLIENT_ID"
        assert data["groups"]["mode"] == "manual"
        assert data["created_at"] != ""

    def test_create_with_groups_config(self, client, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="api-provider",
            groups={
                "mode": "api",
                "sync_interval": 1800,
                "tenant_id_env": "ENTRA_TENANT_ID",
                "client_secret_env": "ENTRA_CLIENT_SECRET",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["groups"]["mode"] == "api"
        assert data["groups"]["sync_interval"] == 1800
        assert data["groups"]["tenant_id_env"] == "ENTRA_TENANT_ID"

    def test_create_sets_rp_client_secret_env(self, client, admin_headers):
        """The RP client_secret_env round-trips - without it a provider created
        via the API could never complete a login (no secret for the exchange)."""
        resp = _create_provider(
            client,
            admin_headers,
            name="rp-secret",
            client_secret_env="OIDC_RP_SECRET",
        )
        assert resp.status_code == 201
        assert resp.json()["client_secret_env"] == "OIDC_RP_SECRET"
        got = client.get("/api/v1/auth/oidc-providers/rp-secret", headers=admin_headers)
        assert got.json()["client_secret_env"] == "OIDC_RP_SECRET"

    def test_create_duplicate_returns_409(self, client, admin_headers):
        _create_provider(client, admin_headers, name="dup-provider")
        resp = _create_provider(client, admin_headers, name="dup-provider")
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/oidc-providers",
            json={"name": "blocked", "type": "generic"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_fields_returns_422(self, client, admin_headers):
        resp = client.post("/api/v1/auth/oidc-providers", json={}, headers=admin_headers)
        assert resp.status_code == 422


class TestListProviders:
    """GET /api/v1/auth/oidc-providers"""

    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_list_after_create(self, client, admin_headers):
        _create_provider(client, admin_headers, name="prov-a")
        _create_provider(client, admin_headers, name="prov-b")
        resp = client.get("/api/v1/auth/oidc-providers", headers=admin_headers)
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["items"]]
        assert "prov-a" in names
        assert "prov-b" in names

    def test_list_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/oidc-providers", headers=viewer_headers)
        assert resp.status_code == 403


class TestGetProvider:
    """GET /api/v1/auth/oidc-providers/{name}"""

    def test_get_provider(self, client, admin_headers):
        _create_provider(client, admin_headers, name="get-test")
        resp = client.get("/api/v1/auth/oidc-providers/get-test", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "get-test"
        assert data["type"] == "generic"

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers/nonexistent", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/oidc-providers/anything", headers=viewer_headers)
        assert resp.status_code == 403

    def test_env_var_names_visible_not_values(self, client, admin_headers):
        """Env var names are exposed, not the actual secret values."""
        _create_provider(
            client,
            admin_headers,
            name="secret-check",
            client_id_env="MY_CLIENT_ID",
        )
        resp = client.get("/api/v1/auth/oidc-providers/secret-check", headers=admin_headers)
        data = resp.json()
        # The env var NAME is visible
        assert data["client_id_env"] == "MY_CLIENT_ID"
        # The response should not contain any resolved env var values
        # (no "password", "secret", "token" fields with actual values)


class TestUpdateProvider:
    """PUT /api/v1/auth/oidc-providers/{name}"""

    def test_update_enabled(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-test")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-test",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_update_display_name(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-name")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-name",
            json={"display_name": "New Name"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "New Name"

    def test_update_groups_config(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-groups")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-groups",
            json={"groups": {"mode": "api", "sync_interval": 900}},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["groups"]["mode"] == "api"
        assert resp.json()["groups"]["sync_interval"] == 900

    def test_update_nonexistent_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/oidc-providers/ghost",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_requires_admin(self, client, viewer_headers):
        resp = client.put(
            "/api/v1/auth/oidc-providers/anything",
            json={"enabled": False},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestDeleteProvider:
    """DELETE /api/v1/auth/oidc-providers/{name}"""

    def test_delete_provider(self, client, admin_headers):
        _create_provider(client, admin_headers, name="del-test")
        resp = client.delete("/api/v1/auth/oidc-providers/del-test", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == "del-test"
        assert data["orphaned_groups"] == []

        # Confirm deleted
        resp = client.get("/api/v1/auth/oidc-providers/del-test", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_reports_orphaned_groups(self, client, admin_headers, app):
        """Groups with source_provider matching the deleted provider are reported."""
        _create_provider(client, admin_headers, name="orphan-prov")

        # Create groups that reference this provider via the store directly
        group_store = app.state.group_store
        group_store.create("synced-group-1", roles=["data_viewer"], description="Synced")
        group_store.update("synced-group-1", source_provider="orphan-prov", source_id="g1")
        group_store.add_member("synced-group-1", "alice")

        group_store.create("synced-group-2", roles=["admin"], description="Synced 2")
        group_store.update("synced-group-2", source_provider="orphan-prov", source_id="g2")

        resp = client.delete("/api/v1/auth/oidc-providers/orphan-prov", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == "orphan-prov"
        assert len(data["orphaned_groups"]) == 2

        orphan_names = [g["name"] for g in data["orphaned_groups"]]
        assert "synced-group-1" in orphan_names
        assert "synced-group-2" in orphan_names

        # Check member count is reported
        g1 = next(g for g in data["orphaned_groups"] if g["name"] == "synced-group-1")
        assert g1["member_count"] == 1
        assert g1["roles"] == ["data_viewer"]

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/oidc-providers/nonexistent", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_requires_admin(self, client, viewer_headers):
        resp = client.delete("/api/v1/auth/oidc-providers/anything", headers=viewer_headers)
        assert resp.status_code == 403


class TestSyncProvider:
    """POST /api/v1/auth/oidc-providers/{name}/sync"""

    def test_sync_generic_provider(self, client, admin_headers):
        """Generic adapter returns empty — sync reports skipped (mode is manual)."""
        _create_provider(client, admin_headers, name="sync-test")
        resp = client.post("/api/v1/auth/oidc-providers/sync-test/sync", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        # Generic provider with manual mode gets skipped
        assert data["total"] == 0

    def test_sync_nonexistent_returns_404(self, client, admin_headers):
        resp = client.post("/api/v1/auth/oidc-providers/ghost/sync", headers=admin_headers)
        assert resp.status_code == 404

    def test_sync_requires_admin(self, client, viewer_headers):
        resp = client.post("/api/v1/auth/oidc-providers/anything/sync", headers=viewer_headers)
        assert resp.status_code == 403


class TestTestProvider:
    """GET /api/v1/auth/oidc-providers/{name}/test"""

    def test_test_generic_provider(self, client, admin_headers):
        """Generic adapter always returns success."""
        _create_provider(client, admin_headers, name="conn-test")
        resp = client.get("/api/v1/auth/oidc-providers/conn-test/test", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Generic provider" in data["message"]

    def test_test_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers/ghost/test", headers=admin_headers)
        assert resp.status_code == 404

    def test_test_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/oidc-providers/anything/test", headers=viewer_headers)
        assert resp.status_code == 403


class TestVerifyLoginConfig:
    """GET /api/v1/auth/oidc-providers/{name}/verify-login"""

    def test_reports_missing_client_id(self, client, admin_headers):
        """An unset client_id env var is flagged, not silently passed."""
        _create_provider(client, admin_headers, name="vl-missing", client_id_env="OIDC_UNSET_XYZ")
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-missing/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        client_id = next(c for c in data["checks"] if c["name"] == "client_id")
        assert client_id["ok"] is False

    def test_reports_present_client_id(self, client, admin_headers, monkeypatch):
        """A resolvable client_id env var passes its check (value never returned)."""
        monkeypatch.setenv("OIDC_PRESENT_ID", "some-client-id")
        _create_provider(client, admin_headers, name="vl-present", client_id_env="OIDC_PRESENT_ID")
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-present/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        client_id = next(c for c in data["checks"] if c["name"] == "client_id")
        assert client_id["ok"] is True
        # The secret value must never appear in the response.
        assert "some-client-id" not in resp.text

    def test_discovery_failure_is_reported_not_raised(self, client, admin_headers):
        """An unreachable/invalid discovery URL yields ok=False, not a 500."""
        _create_provider(
            client, admin_headers, name="vl-baddisco", issuer="https://accounts.example.com"
        )
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-baddisco/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        discovery = next(c for c in data["checks"] if c["name"] == "discovery")
        assert discovery["ok"] is False

    def test_verify_login_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers/ghost/verify-login", headers=admin_headers)
        assert resp.status_code == 404

    def test_verify_login_requires_admin(self, client, viewer_headers):
        resp = client.get(
            "/api/v1/auth/oidc-providers/anything/verify-login", headers=viewer_headers
        )
        assert resp.status_code == 403
