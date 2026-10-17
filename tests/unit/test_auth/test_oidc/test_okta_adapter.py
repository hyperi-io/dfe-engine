#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_okta_adapter.py
#  Purpose:      Tests for OktaAdapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import json

import pytest

from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitProvisioner
from dfe_engine.auth.membership import linked_groups
from dfe_engine.auth.oidc.adapters.base import (
    DirectoryCredentialError,
    DirectoryNotConfiguredError,
)
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import sync_provider
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings
from tests.unit.test_auth.factories import OKTA_API_TOKEN_ENV, make_okta_directory_provider
from tests.unit.test_auth.test_oidc.local_okta import LocalOkta, okta_adapter
from tests.unit.test_auth.test_oidc.unreachable_store import UnreachableSecretStore


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


class TestOktaSecretStoreDidNotAnswer:
    """A token whose secret store is down exists all the same, so it is an error, not 'not configured'."""

    @staticmethod
    def _adapter(monkeypatch) -> OktaAdapter:
        monkeypatch.delenv("OKTA_API_TOKEN", raising=False)
        provider = _okta_provider(mode="api")
        provider.groups.okta_domain = "example.okta.com"
        provider.groups.api_token_path = "oidc/acme/groups_api_token"
        provider.groups.api_token_env = "OKTA_API_TOKEN"
        return OktaAdapter(provider, secrets=UnreachableSecretStore())

    async def test_the_listing_fails_as_an_error_and_the_login_lookup_still_answers(
        self, monkeypatch
    ):
        adapter = self._adapter(monkeypatch)

        with pytest.raises(DirectoryCredentialError):
            await adapter.list_all_groups()
        assert await adapter.resolve_user_groups("00u1") == []

    async def test_the_connection_test_names_the_secret_store(self, monkeypatch):
        ok, message = await self._adapter(monkeypatch).test_connection()

        assert ok is False
        assert "secret store did not answer" in message
        assert "not configured" not in message


SOC_GROUP = {"id": "00g1soc", "profile": {"name": "SOC Analysts", "description": "SOC"}}


class TestOktaLinksOnTheGroupName:
    """Okta's groups claim carries names, so the sync and the login lookup link on names too."""

    async def test_a_synced_group_takes_the_name_a_login_claim_carries(self, tmp_path):
        okta = LocalOkta(pages=[[SOC_GROUP]])
        okta.start()
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("okta", OIDCProvider(type="okta", groups=GroupResolutionConfig(mode="api")))
        group_store = GroupStore(tmp_path / "groups")
        try:
            result = await sync_provider(
                "okta", registry, group_store, adapter=okta_adapter(okta.base_url)
            )
        finally:
            okta.stop()

        assert (result["created"], result["status"]) == (1, "ok")
        synced = group_store.get("soc-analysts")
        assert (synced.source_provider, synced.source_id) == ("okta", "SOC Analysts")
        assert linked_groups(["SOC Analysts"], group_store.list(), {"okta"}) == [synced]
        assert linked_groups(["00g1soc"], group_store.list(), {"okta"}) == []

    async def test_a_claim_login_reaches_the_synced_group_once_it_has_a_role(self, tmp_path):
        okta = LocalOkta(pages=[[SOC_GROUP]])
        okta.start()
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("okta", OIDCProvider(type="okta", groups=GroupResolutionConfig(mode="api")))
        group_store = GroupStore(tmp_path / "groups")
        try:
            await sync_provider("okta", registry, group_store, adapter=okta_adapter(okta.base_url))
        finally:
            okta.stop()
        group_store.update("soc-analysts", roles=["data_analyst"])
        jit = JitProvisioner(
            account_store=AccountStore(tmp_path / "accounts"), group_store=group_store
        )

        account = jit.ensure_account("00u1jane", ["SOC Analysts"], "okta")

        assert account.groups == ["SOC Analysts"]
        assert jit.resolve_hyperdx_team(["SOC Analysts"], "okta") == "dfe-analysts"

    async def test_a_login_lookup_answers_with_group_names(self):
        okta = LocalOkta(pages=[[SOC_GROUP]])
        okta.start()
        try:
            groups = await okta_adapter(okta.base_url).resolve_user_groups("jane@example.com")
        finally:
            okta.stop()

        assert [group.id for group in groups] == ["SOC Analysts"]
        assert okta.pages_requested == [0]


class TestOktaLoginLookupThatFailsPartWay:
    """A page Okta did not serve is no answer, never the groups read before it."""

    async def test_a_page_that_is_not_a_list_returns_no_groups_and_says_why(self, audit_events):
        okta = LocalOkta(pages=[[SOC_GROUP], [SOC_GROUP], [SOC_GROUP]])
        okta.malformed_page = 1
        okta.start()
        try:
            groups = await okta_adapter(okta.base_url).resolve_user_groups("jane@example.com")
        finally:
            okta.stop()

        assert groups == []
        assert okta.pages_requested == [0, 1]
        (line,) = [e for e in audit_events if e["event"].startswith("Okta resolve_user_groups")]
        assert line["error_type"] == "DirectoryPageError"


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


class TestOktaLoginLookupWithATokenNoHeaderCanCarry:
    """A token that is set but never sent finds no groups at login, so the log says which setting to fix."""

    async def test_it_finds_no_groups_and_names_the_setting(
        self, audit_events: list[dict], monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(OKTA_API_TOKEN_ENV, "test-okta\napi-token")
        adapter = OktaAdapter(provider=make_okta_directory_provider(okta_domain="example.okta.com"))

        groups = await adapter.resolve_user_groups("00u1")

        logged = [
            (event["operation"], event["env_var"], event["store_path"])
            for event in audit_events
            if event["event"]
            == "Okta API token not sent: it holds a character an HTTP header cannot carry"
        ]
        leaked = [event for event in audit_events if "api-token" in json.dumps(event, default=str)]
        assert (groups, logged, leaked) == (
            [],
            [("resolve_user_groups", OKTA_API_TOKEN_ENV, "")],
            [],
        )
