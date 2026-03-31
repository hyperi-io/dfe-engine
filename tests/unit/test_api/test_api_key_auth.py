"""Tests for API key authentication path in get_current_user()."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token


@pytest.fixture
def test_api_key(app) -> str:
    """Create a test API key and return the full key string."""
    store = app.state.api_key_store
    group_store = app.state.group_store
    # Create a group for the key (may already exist from bootstrap)
    if group_store.get("test-infra-ops") is None:
        group_store.create("test-infra-ops", roles=["infra_admin"])
    _, full_key = store.create("test-ci-key", groups=["test-infra-ops"])
    return full_key


class TestApiKeyAuthentication:
    """X-API-Key header authenticates via APIKeyStore."""

    def test_valid_api_key_authenticates(self, client: TestClient, test_api_key: str):
        """A valid API key returns an authenticated AuthContext."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-API-Key": test_api_key},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "apikey:test-ci-key"
        assert "infra_admin" in data["roles"]
        assert "test-infra-ops" in data["groups"]

    def test_invalid_api_key_returns_401(self, client: TestClient):
        """An invalid API key returns 401, not a fallthrough to JWT."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-API-Key": "dfe_ak_badshort_badlongtoken00000000000000"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["code"] == "unauthorized"

    def test_malformed_api_key_returns_401(self, client: TestClient):
        """A malformed API key (wrong format) returns 401."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-API-Key": "not-a-valid-key"},
        )
        assert resp.status_code == 401

    def test_api_key_precedence_over_bearer(
        self, client: TestClient, test_api_key: str, api_settings
    ):
        """API key takes precedence over Bearer token when both present."""
        jwt_token = create_access_token(
            data={"sub": "jwt-user", "org_id": "jwt-org", "roles": ["admin"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-API-Key": test_api_key,
                "Authorization": f"Bearer {jwt_token}",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # API key wins over JWT
        assert data["user_id"] == "apikey:test-ci-key"

    def test_oidc_takes_precedence_over_api_key(self, client: TestClient, test_api_key: str):
        """OIDC headers take precedence over API key."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "oidc-user@example.com",
                "X-API-Key": test_api_key,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # OIDC wins over API key
        assert data["user_id"] == "oidc-user@example.com"
