#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_generic_adapter.py
#  Purpose:      Tests for GenericAdapter and OIDCGroupAdapter factory
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import re

import pytest

from dfe_engine.auth.oidc.adapters import get_adapter
from dfe_engine.auth.oidc.adapters.base import DirectoryError
from dfe_engine.auth.oidc.adapters.generic import GenericAdapter
from dfe_engine.auth.oidc.models import OIDCProvider
from tests.unit.test_auth.factories import make_generic_adapter, make_oidc_provider


def _generic_provider() -> OIDCProvider:
    return OIDCProvider(
        type="generic",
        issuer="https://sso.example.com",
        client_id_env="SSO_CLIENT_ID",
    )


class TestGenericAdapterResolveGroups:
    async def test_resolve_groups_returns_empty_list(self):
        adapter = GenericAdapter(_generic_provider())
        result = await adapter.resolve_groups("user@example.com")
        assert result == []

    async def test_resolve_groups_with_empty_subject(self):
        adapter = GenericAdapter(_generic_provider())
        result = await adapter.resolve_groups("")
        assert result == []

    async def test_resolve_groups_returns_list_type(self):
        adapter = GenericAdapter(_generic_provider())
        result = await adapter.resolve_groups("any-subject")
        assert isinstance(result, list)


class TestGenericAdapterListAllGroups:
    async def test_no_directory(self):
        provider = make_oidc_provider(groups={"mode": "api"}, issuer="https://sso.example.com")
        message = (
            "generic directory: there is no directory API to list groups from; set the "
            "provider's groups mode to 'token_claim' or 'manual'"
        )
        with pytest.raises(DirectoryError, match=re.escape(message)):
            await make_generic_adapter(provider=provider).list_all_groups()


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
        # Google adapter falls back to GenericAdapter until dedicated impl is added
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
