"""Tests for auth router — login, me, permissions, 401/403."""

from fastapi.testclient import TestClient


class TestLogin:
    """POST /api/v1/auth/login"""

    def test_login_success(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-pw"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["token_type"] == "bearer"
        assert data["access_token"]
        assert data["user_id"] == "admin"
        assert "admin" in data["roles"]
        assert data["expires_in"] == 30 * 60

    def test_login_bad_password(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["code"] == "unauthorized"

    def test_login_unknown_user(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "test"},
        )
        assert resp.status_code == 401

    def test_login_all_roles(self, client: TestClient):
        """All three local accounts should authenticate."""
        for user, pw in [
            ("admin", "test-admin-pw"),
            ("operator", "test-operator-pw"),
            ("viewer", "test-viewer-pw"),
        ]:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": user, "password": pw},
            )
            assert resp.status_code == 200, f"Login failed for {user}"
            assert resp.json()["user_id"] == user


class TestRefresh:
    """POST /api/v1/auth/refresh"""

    def test_refresh_success(self, client: TestClient, admin_headers: dict):
        resp = client.post("/api/v1/auth/refresh", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["user_id"] == "admin"

    def test_refresh_no_token(self, client: TestClient):
        resp = client.post("/api/v1/auth/refresh")
        assert resp.status_code == 401


class TestMe:
    """GET /api/v1/auth/me"""

    def test_me_authenticated(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin"
        assert data["org_id"] == "test-org"
        assert "admin" in data["roles"]

    def test_me_no_token(self, client: TestClient):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, client: TestClient):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token-xyz"},
        )
        assert resp.status_code == 401


class TestPermissions:
    """GET /api/v1/auth/permissions"""

    def test_admin_has_wildcard(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/auth/permissions", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "*" in data["permissions"]
        assert "admin" in data["roles"]

    def test_viewer_has_limited_perms(self, client: TestClient, viewer_headers: dict):
        resp = client.get("/api/v1/auth/permissions", headers=viewer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "config:read" in data["permissions"]
        assert "config:write" not in data["permissions"]
