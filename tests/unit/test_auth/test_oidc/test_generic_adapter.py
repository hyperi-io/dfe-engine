#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_generic_adapter.py
#  Purpose:      Tests for GenericAdapter and OIDCGroupAdapter factory
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.adapters import get_adapter
from dfe_engine.auth.oidc.adapters.base import NoDirectoryApiError
from dfe_engine.auth.oidc.adapters.generic import GenericAdapter
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import sync_provider


def _generic_provider() -> OIDCProvider:
    return OIDCProvider(
        type="generic",
        issuer="https://sso.example.com",
        client_id_env="SSO_CLIENT_ID",
    )


class TestGenericAdapterListAllGroups:
    async def test_list_all_groups_refuses_because_there_is_no_directory(self):
        adapter = GenericAdapter(_generic_provider())
        with pytest.raises(NoDirectoryApiError):
            await adapter.list_all_groups()

    async def test_an_api_mode_sync_is_an_error_not_an_empty_directory(self, tmp_path):
        """A generic provider set to api mode has no directory, so 'ok, 0 groups' would mislead."""
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create(
            "corp-sso",
            OIDCProvider(
                type="generic",
                issuer="https://sso.example.com",
                groups=GroupResolutionConfig(mode="api"),
            ),
        )
        group_store = GroupStore(tmp_path / "groups")

        result = await sync_provider("corp-sso", registry, group_store)

        stored = registry.get("corp-sso")
        assert stored is not None
        assert (result["status"], stored.last_sync_status) == ("error", "error")
        assert (
            stored.sync_error
            == result["error"]
            == (
                "This provider type has no directory API to list groups from; "
                "set groups.mode to token_claim"
            )
        )
        assert group_store.list() == []


class TestGenericAdapterTestConnection:
    async def test_test_connection_returns_success_true(self):
        adapter = GenericAdapter(_generic_provider())
        success, message = await adapter.test_connection()
        assert success is True

    async def test_test_connection_returns_informational_message(self):
        adapter = GenericAdapter(_generic_provider())
        success, message = await adapter.test_connection()
        assert "Generic provider" in message
        assert "no API to test" in message

    async def test_test_connection_returns_tuple(self):
        adapter = GenericAdapter(_generic_provider())
        result = await adapter.test_connection()
        assert isinstance(result, tuple)
        assert len(result) == 2


class TestGetAdapterFactory:
    def test_generic_type_returns_generic_adapter(self):
        provider = OIDCProvider(type="generic", issuer="https://sso.example.com")
        adapter = get_adapter(provider)
        assert isinstance(adapter, GenericAdapter)

    def test_google_type_returns_adapter(self):
        provider = OIDCProvider(type="google", issuer="https://accounts.google.com")
        adapter = get_adapter(provider)
        assert adapter is not None

    def test_entra_id_type_returns_adapter(self):
        provider = OIDCProvider(
            type="entra_id",
            issuer="https://login.microsoftonline.com/tenant/v2.0",
        )
        adapter = get_adapter(provider)
        assert adapter is not None

    def test_okta_type_returns_adapter(self):
        provider = OIDCProvider(type="okta", issuer="https://dev-123.okta.com")
        adapter = get_adapter(provider)
        assert adapter is not None

    def test_adapter_has_provider_reference(self):
        provider = OIDCProvider(type="generic", issuer="https://sso.example.com")
        adapter = get_adapter(provider)
        assert adapter._provider is provider
