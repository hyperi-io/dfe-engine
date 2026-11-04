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
from dfe_engine.auth.oidc.adapters.entra import EntraAdapter
from dfe_engine.auth.oidc.adapters.generic import GenericAdapter
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
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
    async def test_reports_there_is_nothing_to_test(self):
        result = await GenericAdapter(_generic_provider()).test_connection()
        assert result == (True, "Generic provider -- no API to test")


class TestGetAdapterFactory:
    @pytest.mark.parametrize(
        ("provider_type", "adapter_cls"),
        [
            ("generic", GenericAdapter),
            ("google", GoogleAdapter),
            ("entra_id", EntraAdapter),
            ("okta", OktaAdapter),
        ],
    )
    def test_the_type_picks_its_adapter(self, provider_type, adapter_cls):
        provider = OIDCProvider(type=provider_type, issuer="https://sso.example.com")
        adapter = get_adapter(provider)
        assert type(adapter) is adapter_cls
        assert adapter._provider is provider
