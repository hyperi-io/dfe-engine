#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_crud.py
#  Purpose:      Tests for account CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/PUT/DELETE /api/v1/auth/accounts endpoints."""

from __future__ import annotations


class TestCreateAccount:
    """POST /api/v1/auth/accounts"""

    def test_create_account(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={"username": "newuser", "password": "s3cret", "groups": ["dfe-viewers"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "newuser"
        assert data["enabled"] is True
        assert data["groups"] == ["dfe-viewers"]
        assert "password_hash" not in data

        group = client.get("/api/v1/auth/groups/dfe-viewers", headers=admin_headers)
        assert "newuser" in group.json()["members"]

    def test_create_duplicate_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "dupuser", "password": "pw1"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/accounts",
            json={"username": "dupuser", "password": "pw2"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={"username": "blocked", "password": "pw"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_fields_returns_422(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 422


class TestListAccounts:
    """GET /api/v1/auth/accounts"""

    def test_list_accounts(self, client, admin_headers):
        resp = client.get("/api/v1/auth/accounts", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()["items"]
        assert isinstance(data, list)
        # At minimum, admin account exists from bootstrap
        assert any(a["username"] == "admin" for a in data)
        # No password hashes leaked
        for account in data:
            assert "password_hash" not in account

    def test_list_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/accounts", headers=viewer_headers)
        assert resp.status_code == 403


class TestGetAccount:
    """GET /api/v1/auth/accounts/{username}"""

    def test_get_account(self, client, admin_headers):
        resp = client.get("/api/v1/auth/accounts/admin", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert "password_hash" not in data

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/accounts/nonexistent", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/accounts/admin", headers=viewer_headers)
        assert resp.status_code == 403


class TestUpdateAccount:
    """PUT /api/v1/auth/accounts/{username}"""

    def test_update_groups(self, client, admin_headers):
        # Create account first
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "updatable", "password": "pw"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/accounts/updatable",
            json={"groups": ["dfe-analysts"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["groups"] == ["dfe-analysts"]

        group = client.get("/api/v1/auth/groups/dfe-analysts", headers=admin_headers)
        assert "updatable" in group.json()["members"]

    def test_update_groups_removes_from_old_group(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "grp-sync",
                "password": "pw",
                "groups": ["dfe-viewers"],
            },
            headers=admin_headers,
        )
        client.put(
            "/api/v1/auth/accounts/grp-sync",
            json={"groups": ["dfe-analysts"]},
            headers=admin_headers,
        )
        viewers = client.get("/api/v1/auth/groups/dfe-viewers", headers=admin_headers)
        analysts = client.get("/api/v1/auth/groups/dfe-analysts", headers=admin_headers)
        assert "grp-sync" not in viewers.json()["members"]
        assert "grp-sync" in analysts.json()["members"]

    def test_update_enabled(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "disableme", "password": "pw"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/accounts/disableme",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_update_nonexistent_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/accounts/ghost",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_requires_admin(self, client, viewer_headers):
        resp = client.put(
            "/api/v1/auth/accounts/admin",
            json={"enabled": False},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestResetPassword:
    """POST /api/v1/auth/accounts/{username}/reset-password"""

    def test_reset_password(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "pwreset", "password": "oldpw"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/accounts/pwreset/reset-password",
            json={"new_password": "newpw"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["message"] == "password reset"

    def test_reset_to_current_password_rejected(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "pwsame", "password": "samepw"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/accounts/pwsame/reset-password",
            json={"new_password": "samepw"},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "password_reused"
        # Message must not reveal that the match was the current password.
        assert "current" not in body["message"].lower()

    def test_reset_nonexistent_returns_404(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts/ghost/reset-password",
            json={"new_password": "pw"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_reset_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/accounts/admin/reset-password",
            json={"new_password": "pw"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestDeleteAccount:
    """DELETE /api/v1/auth/accounts/{username}"""

    def test_delete_account(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "deleteme", "password": "pw"},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/auth/accounts/deleteme", headers=admin_headers)
        assert resp.status_code == 204

        # Confirm it's gone
        resp = client.get("/api/v1/auth/accounts/deleteme", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/accounts/ghost", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_requires_admin(self, client, viewer_headers):
        resp = client.delete("/api/v1/auth/accounts/admin", headers=viewer_headers)
        assert resp.status_code == 403
