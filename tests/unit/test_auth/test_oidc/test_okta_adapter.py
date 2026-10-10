#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_okta_adapter.py
#  Purpose:      Tests for OktaAdapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.idp_errors import DirectoryNotConfiguredError
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings


def _okta_provider(*, mode: str = "token_claim") -> OIDCProvider:
    return OIDCProvider(
        type="okta",
        issuer="https://example.okta.com",
        client_id_env="OKTA_CLIENT_ID",
        client_secret_env="OKTA_CLIENT_SECRET",
        groups=GroupResolutionConfig(mode=mode, claim_name="groups"),
    )


class TestOktaAdapterTestConnection:
    async def test_token_claim_mode_succeeds_without_api(self):
        adapter = OktaAdapter(_okta_provider(mode="token_claim"))
        ok, message = await adapter.test_connection()
        assert ok is True
        assert "token_claim" in message
        assert "verify-login" in message

    async def test_api_mode_missing_domain_fails(self):
        adapter = OktaAdapter(_okta_provider(mode="api"))
        ok, message = await adapter.test_connection()
        assert ok is False
        assert "okta_domain" in message

    async def test_api_mode_missing_token_fails(self, monkeypatch):
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_env = "OKTA_API_TOKEN"
        monkeypatch.delenv("OKTA_API_TOKEN", raising=False)
        adapter = OktaAdapter(provider)
        ok, message = await adapter.test_connection()
        assert ok is False
        assert "OKTA_API_TOKEN" in message


class TestOktaListAllGroupsNoCredentials:
    async def test_refuses_to_list_without_a_domain(self):
        adapter = OktaAdapter(_okta_provider(mode="api"))
        with pytest.raises(DirectoryNotConfiguredError):
            await adapter.list_all_groups()

    async def test_refuses_to_list_without_a_token(self, monkeypatch):
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_env = "OKTA_API_TOKEN"
        monkeypatch.delenv("OKTA_API_TOKEN", raising=False)
        adapter = OktaAdapter(provider)
        with pytest.raises(DirectoryNotConfiguredError):
            await adapter.list_all_groups()


class TestOktaApiTokenResolution:
    """The Groups API token resolves through the secret store before the env."""

    def _store(self, tmp_path):
        return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))

    def test_stored_token_beats_the_env(self, tmp_path, monkeypatch):
        store = self._store(tmp_path)
        store.put("oidc/acme/groups_api_token", "from-store")
        monkeypatch.setenv("OKTA_API_TOKEN", "from-env")
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_path = "oidc/acme/groups_api_token"
        provider.groups.api_token_env = "OKTA_API_TOKEN"

        _, headers = OktaAdapter(provider, secrets=store)._api_base_and_headers()

        assert headers["Authorization"] == "SSWS from-store"

    def test_env_serves_when_nothing_is_stored(self, tmp_path, monkeypatch):
        store = self._store(tmp_path)
        monkeypatch.setenv("OKTA_API_TOKEN", "from-env")
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_path = "oidc/acme/groups_api_token"
        provider.groups.api_token_env = "OKTA_API_TOKEN"

        _, headers = OktaAdapter(provider, secrets=store)._api_base_and_headers()

        assert headers["Authorization"] == "SSWS from-env"
