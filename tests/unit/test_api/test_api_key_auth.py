"""Tests for API key authentication path in get_current_user()."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dfe_engine.yaml_utils as yu
from dfe_engine.api import deps
from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.jit import API_KEY_SUBJECT_PREFIX


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

    def test_unexpired_api_key_authenticates(self, client: TestClient, app):
        """A key with a future expiry authenticates normally."""
        expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        _, full_key = app.state.api_key_store.create("future-key", expires_at=expires_at)
        resp = client.get("/api/v1/auth/me", headers={"X-API-Key": full_key})
        assert resp.status_code == 200

    def test_expired_api_key_returns_401(self, client: TestClient, app):
        """An expired key is rejected at auth time, no sweeper required."""
        store = app.state.api_key_store
        _, full_key = store.create("expiring-key")
        key_file = store._keys_dir / "expiring-key.yaml"
        data = yu.yaml_load(key_file)
        data["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        yu.yaml_dump(data, key_file)

        resp = client.get("/api/v1/auth/me", headers={"X-API-Key": full_key})
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "unauthorized"
        assert body["message"] == "API key expired"

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


class TestAKeyNamingAProviderId:
    """A key's groups resolve by name only, so one naming a group's provider ID holds nothing and is warned about once."""

    def test_it_holds_no_roles_and_is_warned_about_once(
        self, app: FastAPI, audit_events: list[dict], client: TestClient
    ):
        identifier = f"okta-admins-{secrets.token_hex(4)}"
        app.state.group_store.update(
            name="dfe-admins", source_id=identifier, source_provider="oidc"
        )
        _, full_key = app.state.api_key_store.create(groups=[identifier], name="ci-provider-id")
        headers = {"X-API-Key": full_key}

        first = client.get("/api/v1/auth/me", headers=headers)
        second = client.get("/api/v1/auth/me", headers=headers)

        assert (first.status_code, second.status_code) == (200, 200)
        assert (first.json()["roles"], second.json()["roles"]) == ([], [])
        warned = [event for event in audit_events if event.get("identifier") == identifier]
        assert [(event["event"], event["group"]) for event in warned] == [
            ("API key names a group's provider ID, which grants nothing", "dfe-admins")
        ]


def test_the_api_key_subject_prefix_has_one_spelling():
    """JIT refuses an IdP subject by this prefix and deps routes a key by it, so both read it."""
    source = Path(deps.__file__).read_text(encoding="utf-8")

    assert f'"{API_KEY_SUBJECT_PREFIX}' not in source
