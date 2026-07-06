#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_api_key_crud.py
#  Purpose:      Tests for API key CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/DELETE /api/v1/auth/api-keys endpoints."""

from __future__ import annotations


class TestCreateAPIKey:
    """POST /api/v1/auth/api-keys"""

    def test_create_api_key(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "test-key", "groups": ["dfe-viewers"], "description": "CI key"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-key"
        assert data["enabled"] is True
        assert data["groups"] == ["dfe-viewers"]
        assert data["description"] == "CI key"
        # Full key is returned exactly once
        assert "full_key" in data
        assert data["full_key"].startswith("dfe_ak_")
        assert "short_token" in data
        # No key_hash leaked
        assert "key_hash" not in data

    def test_create_duplicate_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/auth/api-keys",
            json={"name": "dup-key"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "dup-key"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "blocked-key"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_name_returns_422(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_api_key_emits_audit(self, client, admin_headers, monkeypatch):
        import dfe_engine.api.v1.api_keys as api_keys_mod

        calls = []
        monkeypatch.setattr(api_keys_mod, "audit_resource_change", lambda *a: calls.append(a))
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "audit-key"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        short_token = resp.json()["short_token"]
        assert calls == [("admin", "api_key", "audit-key", "created", {"short_token": short_token})]


class TestListAPIKeys:
    """GET /api/v1/auth/api-keys"""

    def test_list_api_keys(self, client, admin_headers):
        # Create a key first
        client.post(
            "/api/v1/auth/api-keys",
            json={"name": "list-key"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/auth/api-keys", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(k["name"] == "list-key" for k in data)
        # No sensitive data leaked
        for key in data:
            assert "key_hash" not in key
            assert "full_key" not in key

    def test_list_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/api-keys", headers=viewer_headers)
        assert resp.status_code == 403


class TestRevokeAPIKey:
    """DELETE /api/v1/auth/api-keys/{short_token}"""

    def test_revoke_api_key(self, client, admin_headers):
        # Create a key and get the short_token
        create_resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "revoke-key"},
            headers=admin_headers,
        )
        short_token = create_resp.json()["short_token"]

        resp = client.delete(f"/api/v1/auth/api-keys/{short_token}", headers=admin_headers)
        assert resp.status_code == 204

        # Confirm it's gone from the list
        list_resp = client.get("/api/v1/auth/api-keys", headers=admin_headers)
        names = [k["name"] for k in list_resp.json()]
        assert "revoke-key" not in names

    def test_revoke_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/api-keys/deadbeef", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_revoke_requires_admin(self, client, viewer_headers):
        resp = client.delete("/api/v1/auth/api-keys/deadbeef", headers=viewer_headers)
        assert resp.status_code == 403

    def test_revoke_api_key_emits_audit(self, client, admin_headers, monkeypatch):
        create_resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "audit-revoke-key"},
            headers=admin_headers,
        )
        short_token = create_resp.json()["short_token"]

        import dfe_engine.api.v1.api_keys as api_keys_mod

        calls = []
        monkeypatch.setattr(api_keys_mod, "audit_resource_change", lambda *a: calls.append(a))
        resp = client.delete(f"/api/v1/auth/api-keys/{short_token}", headers=admin_headers)
        assert resp.status_code == 204
        assert calls == [("admin", "api_key", short_token, "revoked")]
