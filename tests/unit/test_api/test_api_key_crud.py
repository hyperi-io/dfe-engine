#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_api_key_crud.py
#  Purpose:      Tests for API key CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/DELETE /api/v1/auth/api-keys endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _iso_in(**delta) -> str:
    return (datetime.now(UTC) + timedelta(**delta)).isoformat()


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

    def test_create_without_expiry_takes_the_default_lifetime(self, client, admin_headers):
        before = datetime.now(UTC)
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "default-expiry-key"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        expiry = datetime.fromisoformat(data["expires_at"])
        assert before + timedelta(days=90) <= expiry <= datetime.now(UTC) + timedelta(days=90)
        assert data["expired"] is False

    def test_a_configured_lifetime_is_the_default(self, client, app, admin_headers):
        app.state.settings.auth.api_key_default_ttl_days = 7
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "week-key"},
            headers=admin_headers,
        )
        expiry = datetime.fromisoformat(resp.json()["expires_at"])
        assert expiry <= datetime.now(UTC) + timedelta(days=7)
        assert expiry > datetime.now(UTC) + timedelta(days=6)

    def test_a_zero_lifetime_creates_a_key_that_never_expires(self, client, app, admin_headers):
        app.state.settings.auth.api_key_default_ttl_days = 0
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "no-expiry-key"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["expires_at"] is None
        assert resp.json()["expired"] is False

    def test_an_explicit_expiry_beats_the_default(self, client, admin_headers):
        expires_at = _iso_in(days=365)
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "year-key", "expires_at": expires_at},
            headers=admin_headers,
        )
        assert datetime.fromisoformat(resp.json()["expires_at"]) == datetime.fromisoformat(
            expires_at
        )

    def test_create_with_expiry(self, client, admin_headers):
        expires_at = _iso_in(days=30)
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "expiring-key", "expires_at": expires_at},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert datetime.fromisoformat(data["expires_at"]) == datetime.fromisoformat(expires_at)
        assert data["expired"] is False

    def test_create_with_past_expiry_returns_400(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "stale-key", "expires_at": _iso_in(days=-1)},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"

    def test_create_with_unparseable_expiry_returns_400(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/api-keys",
            json={"name": "bad-expiry-key", "expires_at": "next tuesday"},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "validation_error"

    def test_rejected_expiry_creates_no_key(self, client, admin_headers):
        client.post(
            "/api/v1/auth/api-keys",
            json={"name": "rejected-key", "expires_at": "not-a-date"},
            headers=admin_headers,
        )
        list_resp = client.get("/api/v1/auth/api-keys", headers=admin_headers)
        names = [k["name"] for k in list_resp.json()["items"]]
        assert "rejected-key" not in names


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
        data = resp.json()["items"]
        assert isinstance(data, list)
        assert any(k["name"] == "list-key" for k in data)
        # No sensitive data leaked
        for key in data:
            assert "key_hash" not in key
            assert "full_key" not in key

    def test_list_reports_expiry_state(self, client, admin_headers, app):
        client.post(
            "/api/v1/auth/api-keys",
            json={"name": "lapsed-key", "expires_at": _iso_in(seconds=60)},
            headers=admin_headers,
        )
        # Backdate the stored expiry — create() only accepts future values.
        import dfe_engine.yaml_utils as yu

        key_file = app.state.api_key_store._keys_dir / "lapsed-key.yaml"
        data = yu.yaml_load(key_file)
        data["expires_at"] = _iso_in(days=-1)
        yu.yaml_dump(data, key_file)

        resp = client.get("/api/v1/auth/api-keys", headers=admin_headers)
        assert resp.status_code == 200
        lapsed = next(k for k in resp.json()["items"] if k["name"] == "lapsed-key")
        assert lapsed["expired"] is True
        assert lapsed["expires_at"] is not None

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
        names = [k["name"] for k in list_resp.json()["items"]]
        assert "revoke-key" not in names

    def test_revoke_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/api-keys/deadbeef", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_revoke_requires_admin(self, client, viewer_headers):
        resp = client.delete("/api/v1/auth/api-keys/deadbeef", headers=viewer_headers)
        assert resp.status_code == 403
