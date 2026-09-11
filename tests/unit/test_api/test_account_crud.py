#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_account_crud.py
#  Purpose:      Tests for account CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/PUT/DELETE /api/v1/auth/accounts endpoints."""

from __future__ import annotations

import pytest


class TestCreateAccount:
    """POST /api/v1/auth/accounts"""

    def test_create_account(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "newuser",
                "password": "s3cret",
                "email": "newuser@example.com",
                "groups": ["dfe-viewers"],
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"] == "newuser"
        assert data["enabled"] is True
        assert data["groups"] == ["dfe-viewers"]
        assert "password_hash" not in data
        assert data["email"] == "newuser@example.com"
        assert data["phone"] == ""
        assert data["name"] == ""

        group = client.get("/api/v1/auth/groups/dfe-viewers", headers=admin_headers)
        assert "newuser" in group.json()["members"]

    def test_create_account_with_contact_fields(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "with-contact",
                "password": "s3cret",
                "email": "with-contact@example.com",
                "phone": "+15551212",
                "name": "With Contact",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "with-contact@example.com"
        assert data["phone"] == "+15551212"
        assert data["name"] == "With Contact"

        got = client.get("/api/v1/auth/accounts/with-contact", headers=admin_headers)
        assert got.status_code == 200
        assert got.json()["email"] == "with-contact@example.com"
        assert got.json()["phone"] == "+15551212"
        assert got.json()["name"] == "With Contact"

    def test_create_without_email_returns_422(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={"username": "no-email", "password": "pw"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_empty_email_returns_422(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={"username": "empty-email", "password": "pw", "email": ""},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_with_email_only(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "email-only",
                "password": "pw",
                "email": "only@example.com",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "only@example.com"
        assert data["phone"] == ""
        assert data["name"] == ""

    def test_create_duplicate_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "dupuser", "password": "pw1", "email": "dupuser@example.com"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/auth/accounts",
            json={"username": "dupuser", "password": "pw2", "email": "dupuser@example.com"},
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
            assert "email" in account
            assert "phone" in account
            assert "name" in account

    def test_list_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/accounts", headers=viewer_headers)
        assert resp.status_code == 403

    def test_list_search_matches_name_and_email(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "search-me",
                "password": "pw",
                "email": "needle@example.com",
                "name": "Findable Person",
            },
            headers=admin_headers,
        )
        by_email = client.get(
            "/api/v1/auth/accounts",
            params={"search": "needle@example.com"},
            headers=admin_headers,
        )
        assert by_email.status_code == 200
        assert any(a["username"] == "search-me" for a in by_email.json()["items"])

        by_name = client.get(
            "/api/v1/auth/accounts",
            params={"search": "Findable Person"},
            headers=admin_headers,
        )
        assert by_name.status_code == 200
        assert any(a["username"] == "search-me" for a in by_name.json()["items"])


class TestGetAccount:
    """GET /api/v1/auth/accounts/{username}"""

    def test_get_account(self, client, admin_headers):
        resp = client.get("/api/v1/auth/accounts/admin", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "admin"
        assert "password_hash" not in data
        assert "email" in data
        assert "phone" in data
        assert "name" in data

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
            json={"username": "updatable", "password": "pw", "email": "updatable@example.com"},
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
                "email": "grp-sync@example.com",
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

    def test_update_contact_fields(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "contact-upd",
                "password": "pw",
                "email": "old@example.com",
                "phone": "+1000",
                "name": "Old Name",
            },
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/accounts/contact-upd",
            json={
                "email": "new@example.com",
                "phone": "+2000",
                "name": "New Name",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "new@example.com"
        assert resp.json()["phone"] == "+2000"
        assert resp.json()["name"] == "New Name"

        got = client.get("/api/v1/auth/accounts/contact-upd", headers=admin_headers)
        assert got.json()["email"] == "new@example.com"
        assert got.json()["phone"] == "+2000"
        assert got.json()["name"] == "New Name"

    def test_update_contact_fields_omitted_are_unchanged(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "contact-omit",
                "password": "pw",
                "email": "keep@example.com",
                "phone": "+1111",
                "name": "Keep Me",
            },
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/accounts/contact-omit",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["email"] == "keep@example.com"
        assert data["phone"] == "+1111"
        assert data["name"] == "Keep Me"

    def test_update_contact_fields_empty_string_clears(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "contact-clear",
                "password": "pw",
                "email": "gone@example.com",
                "phone": "+9999",
                "name": "Gone",
            },
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/accounts/contact-clear",
            json={"phone": "", "name": ""},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "gone@example.com"
        assert data["phone"] == ""
        assert data["name"] == ""

    def test_update_empty_email_returns_422(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "email-required",
                "password": "pw",
                "email": "keep@example.com",
            },
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/accounts/email-required",
            json={"email": ""},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_update_enabled(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "disableme", "password": "pw", "email": "disableme@example.com"},
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
            json={"username": "pwreset", "password": "oldpw", "email": "pwreset@example.com"},
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
            json={"username": "pwsame", "password": "samepw", "email": "pwsame@example.com"},
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


class TestRotatePassword:
    """POST /api/v1/auth/accounts/{username}/rotate-password"""

    def test_unwired_secrets_seam_returns_501_with_the_store_command(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts/admin/rotate-password",
            json={"new_password": "a-newly-minted-admin-password"},
            headers=admin_headers,
        )

        assert resp.status_code == 501
        body = resp.json()
        assert body["code"] == "secrets_seam_not_wired"
        # The wording varies with the detected deploy kind; that it names a store
        # operation rather than an engine one is what matters here.
        assert body["context"]["store_command"]

    def test_a_wired_seam_writes_the_password_to_the_store(self, app, client, admin_headers):
        from dfe_engine.secrets import build_secrets

        settings = app.state.settings
        settings.auth.local.admin_password_secret_path = "auth/admin-password"

        resp = client.post(
            "/api/v1/auth/accounts/admin/rotate-password",
            json={"new_password": "a-newly-minted-admin-password"},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["secret_path"] == "auth/admin-password"
        stored = build_secrets(settings.secrets).get("auth/admin-password")
        assert stored == "a-newly-minted-admin-password"

    def test_a_regular_account_is_not_store_backed(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/accounts/viewer/rotate-password",
            json={"new_password": "a-newly-minted-password"},
            headers=admin_headers,
        )

        assert resp.status_code == 400
        assert resp.json()["code"] == "not_store_backed"

    @pytest.mark.parametrize("password", ["", "short", "changeme"])
    def test_a_password_the_engine_cannot_boot_on_is_refused(self, client, admin_headers, password):
        """Rotating to an empty, short or default password bricks the next start."""
        resp = client.post(
            "/api/v1/auth/accounts/admin/rotate-password",
            json={"new_password": password},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["errors"][0]["field"] == "new_password"
        assert body["errors"][0]["message"]

    def test_the_default_password_is_refused_however_it_is_padded(self, client, admin_headers):
        """Long enough to clear the floor, still the password the boot gate refuses."""
        resp = client.post(
            "/api/v1/auth/accounts/admin/rotate-password",
            json={"new_password": "   changeme    "},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert "default admin password" in resp.json()["errors"][0]["message"]

    def test_rotate_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/accounts/admin/rotate-password",
            json={"new_password": "a-newly-minted-password"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestDeleteAccount:
    """DELETE /api/v1/auth/accounts/{username}"""

    def test_delete_account(self, client, admin_headers):
        client.post(
            "/api/v1/auth/accounts",
            json={"username": "deleteme", "password": "pw", "email": "deleteme@example.com"},
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
