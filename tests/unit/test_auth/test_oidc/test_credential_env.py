#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_credential_env.py
#  Purpose:      Tests for OIDC credential resolution (store before env)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.auth.oidc.credential_env import (
    credential_check,
    is_env_var_name,
    provider_secret_path,
    resolve_credential,
)
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


@pytest.fixture
def store(tmp_path):
    """A real scalo.secrets file backend under tmp_path - no mocks."""
    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


class TestCredentialCheck:
    def test_missing_env_name(self):
        ok, detail = credential_check("client_id")
        assert ok is False
        assert "no env var" in detail

    def test_resolved_env(self, monkeypatch):
        monkeypatch.setenv("MY_OIDC_CLIENT_ID", "secret-value")
        ok, detail = credential_check("client_id", env_name="MY_OIDC_CLIENT_ID")
        assert ok is True
        assert "secret-value" not in detail

    def test_unset_valid_env_name(self, monkeypatch):
        monkeypatch.delenv("UNSET_OIDC_ID", raising=False)
        ok, detail = credential_check("client_id", env_name="UNSET_OIDC_ID")
        assert ok is False
        assert "unset or empty" in detail

    def test_literal_okta_client_id_rejected(self):
        ok, detail = credential_check("client_id", env_name="0oa15mxzztuHEzwr7698")
        assert ok is False
        assert "environment variable name" in detail
        assert "0oa" not in detail

    def test_plain_value_passes(self):
        ok, detail = credential_check("client_id", value="0oa15mxzztuHEzwr7698")
        assert ok is True
        assert "0oa" not in detail

    def test_stored_secret_passes_and_names_the_path(self, store):
        store.put("oidc/acme/client_secret", "stored-value")
        ok, detail = credential_check(
            "client_secret", secret_path="oidc/acme/client_secret", secrets=store
        )
        assert ok is True
        assert "oidc/acme/client_secret" in detail
        assert "stored-value" not in detail

    def test_path_with_nothing_stored_and_no_env_is_reported(self, store):
        ok, detail = credential_check(
            "client_secret", secret_path="oidc/acme/client_secret", secrets=store
        )
        assert ok is False
        assert "nothing stored at oidc/acme/client_secret" in detail


class TestResolveCredential:
    def test_store_wins_over_env(self, store, monkeypatch):
        """A secret written through the API beats a stale env var of the same name."""
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        store.put("oidc/acme/client_secret", "from-store")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=store,
        )
        assert resolved == "from-store"

    def test_env_serves_a_provider_with_no_stored_secret(self, store, monkeypatch):
        """An ESO-mounted env var keeps working when the store holds nothing."""
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=store,
        )
        assert resolved == "from-env"

    def test_env_serves_when_no_store_is_wired(self, monkeypatch):
        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        assert resolve_credential(env_name="OIDC_RP_SECRET") == "from-env"

    def test_plain_value_wins_over_both(self, store, monkeypatch):
        monkeypatch.setenv("OIDC_CLIENT_ID", "from-env")
        store.put("oidc/acme/client_secret", "from-store")
        resolved = resolve_credential(
            value="plain-id",
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_CLIENT_ID",
            secrets=store,
        )
        assert resolved == "plain-id"

    def test_nothing_configured_resolves_empty(self, store):
        assert resolve_credential(secrets=store) == ""

    def test_unreachable_store_falls_through_to_env(self, monkeypatch):
        """A store that raises must not take the login path down with it."""

        class _BrokenStore:
            def get(self, path: str) -> str:
                raise RuntimeError("openbao unreachable")

        monkeypatch.setenv("OIDC_RP_SECRET", "from-env")
        resolved = resolve_credential(
            secret_path="oidc/acme/client_secret",
            env_name="OIDC_RP_SECRET",
            secrets=_BrokenStore(),
        )
        assert resolved == "from-env"


class TestPathAndNameHelpers:
    def test_provider_secret_path_shape(self):
        assert provider_secret_path("acme", "client_secret") == "oidc/acme/client_secret"

    @pytest.mark.parametrize("name", ["OKTA_CLIENT_ID", "_private", "A1_b2"])
    def test_env_var_names_accepted(self, name):
        assert is_env_var_name(name) is True

    @pytest.mark.parametrize("name", ["0oa15mxzztuHEzwr7698", "has-dash", "has space", ""])
    def test_non_env_var_names_rejected(self, name):
        assert is_env_var_name(name) is False
