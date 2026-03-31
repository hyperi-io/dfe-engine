"""Tests for OIDC header authentication path in get_current_user()."""

from __future__ import annotations

from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token


class TestOidcAuthentication:
    """OIDC headers (X-Oidc-Subject, X-Oidc-Groups) authenticate without JWT."""

    def test_oidc_subject_authenticates(self, client: TestClient):
        """X-Oidc-Subject header alone is sufficient for authentication."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "alice@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "alice@example.com"

    def test_oidc_groups_resolve_roles(self, client: TestClient, app):
        """Groups from X-Oidc-Groups resolve to roles via GroupStore."""
        # dfe-admins group was seeded by bootstrap with roles=["admin"]
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "alice@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "dfe-admins" in data["groups"]

    def test_unknown_groups_no_roles(self, client: TestClient):
        """Unknown groups authenticate but yield no roles."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "bob@example.com",
                "X-Oidc-Groups": "unknown-group-xyz",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "bob@example.com"
        assert data["roles"] == []
        assert "unknown-group-xyz" in data["groups"]

    def test_multiple_comma_separated_groups(self, client: TestClient, app):
        """Multiple groups are split on comma and all roles collected."""
        # dfe-admins has ["admin"], dfe-analysts has ["data_analyst"]
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "carol@example.com",
                "X-Oidc-Groups": "dfe-admins, dfe-analysts",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_analyst" in data["roles"]
        assert "dfe-admins" in data["groups"]
        assert "dfe-analysts" in data["groups"]

    def test_empty_groups_header(self, client: TestClient):
        """Empty X-Oidc-Groups header authenticates with no roles."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "dave@example.com",
                "X-Oidc-Groups": "",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "dave@example.com"
        assert data["roles"] == []

    def test_oidc_takes_precedence_over_bearer(self, client: TestClient, api_settings, app):
        """OIDC headers take precedence even when a valid Bearer token is present."""
        # Create a JWT with different identity
        jwt_token = create_access_token(
            data={"sub": "jwt-user", "org_id": "jwt-org", "roles": ["admin"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "oidc-user@example.com",
                "Authorization": f"Bearer {jwt_token}",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # OIDC wins — user_id from OIDC, not JWT
        assert data["user_id"] == "oidc-user@example.com"

    def test_oidc_whitespace_in_groups_stripped(self, client: TestClient, app):
        """Whitespace around group names is stripped."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "eve@example.com",
                "X-Oidc-Groups": " dfe-admins , dfe-viewers ",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_viewer" in data["roles"]
