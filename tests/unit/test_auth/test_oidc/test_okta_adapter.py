#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_okta_adapter.py
#  Purpose:      Tests for OktaAdapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider


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
