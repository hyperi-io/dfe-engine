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

    def test_refresh_rejects_disabled_account(self, client: TestClient, admin_headers: dict):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-viewer-pw"},
        )
        assert login.status_code == 200
        viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        disable = client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert disable.status_code == 200

        resp = client.post("/api/v1/auth/refresh", headers=viewer_headers)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Account disabled"

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": True},
            headers=admin_headers,
        )


class TestMe:
    """GET /api/v1/auth/me"""

    def test_me_authenticated(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin"
        assert data["org_id"] == "test-org"
        assert "admin" in data["roles"]
        assert "dfe-admins" in data["groups"]

    def test_me_no_token(self, client: TestClient):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, client: TestClient):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token-xyz"},
        )
        assert resp.status_code == 401

    def test_me_reflects_group_removal_without_relogin(
        self, client: TestClient, admin_headers: dict, api_settings
    ):
        from dfe_engine.api.deps import create_access_token

        client.post(
            "/api/v1/auth/groups",
            json={"name": "me-live-group", "roles": ["admin"], "members": ["viewer"]},
            headers=admin_headers,
        )
        token = create_access_token(
            data={
                "sub": "viewer",
                "org_id": "test-org",
                "roles": ["admin"],
                "groups": ["me-live-group", "dfe-admins"],
            },
            settings=api_settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        before = client.get("/api/v1/auth/me", headers=headers)
        assert "me-live-group" in before.json()["groups"]
        assert "admin" in before.json()["roles"]

        client.delete(
            "/api/v1/auth/groups/me-live-group/members/viewer",
            headers=admin_headers,
        )

        after = client.get("/api/v1/auth/me", headers=headers)
        assert after.status_code == 200
        data = after.json()
        assert "me-live-group" not in data["groups"]
        assert "admin" not in data["roles"]
        assert "data_viewer" in data["roles"]

    def test_me_rejects_disabled_account(self, client: TestClient, admin_headers: dict):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-viewer-pw"},
        )
        assert login.status_code == 200
        viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": False},
            headers=admin_headers,
        )

        resp = client.get("/api/v1/auth/me", headers=viewer_headers)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Account disabled"

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": True},
            headers=admin_headers,
        )


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
        assert "infra_viewer" in data["roles"] or "data_analyst_viewer" in data["roles"]
        assert "source:read" in data["permissions"]
        assert "config:write" not in data["permissions"]

    def test_permissions_ignore_stale_jwt_roles(self, client: TestClient, api_settings):
        """Roles in the JWT are not used; group membership is authoritative."""
        from dfe_engine.api.deps import create_access_token

        token = create_access_token(
            data={
                "sub": "viewer",
                "org_id": "test-org",
                "roles": ["admin"],
                "groups": ["dfe-admins"],
            },
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/permissions",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" not in data["roles"]
        assert "data_viewer" in data["roles"]
        assert "*" not in data["permissions"]

    def test_me_ignores_stale_jwt_when_account_has_no_groups(
        self, client: TestClient, admin_headers: dict, api_settings
    ):
        from dfe_engine.api.deps import create_access_token

        client.post(
            "/api/v1/auth/accounts",
            json={"username": "nogrp", "password": "pw", "groups": []},
            headers=admin_headers,
        )
        token = create_access_token(
            data={
                "sub": "nogrp",
                "org_id": "test-org",
                "roles": ["admin"],
                "groups": ["dfe-admins"],
            },
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["groups"] == []
        assert data["roles"] == []
        assert "admin" not in data["permissions"]
        assert "*" not in data.get("permissions", [])
