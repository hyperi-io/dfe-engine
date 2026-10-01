#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_entra_adapter.py
#  Purpose:      Tests for Entra ID Graph API group adapter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for EntraAdapter.

All tests exercise code paths that do NOT require real Entra credentials.
The "no credentials" paths cover the majority of production safety
logic (fail-open fallback behaviour).

Tests that require a live Entra / Microsoft Graph endpoint are marked
``@pytest.mark.skip(reason="Needs Entra sandbox")``.
"""

from __future__ import annotations

import os

import pytest

from dfe_engine.auth.oidc.adapters.entra import EntraAdapter, graph_credentials
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from tests.unit.test_auth.test_oidc.entra_adapter_cases import (
    GRAPH_CREDENTIALS_CASES,
    GraphCredentialsCase,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_provider(
    *,
    client_id_env: str = "ENTRA_CLIENT_ID",
    tenant_id_env: str = "ENTRA_TENANT_ID",
    client_secret_env: str = "ENTRA_CLIENT_SECRET",  # noqa: S107 — env var name, not a secret
) -> OIDCProvider:
    """Return an Entra OIDCProvider with the given env var names."""
    return OIDCProvider(
        type="entra_id",
        issuer="https://login.microsoftonline.com/test-tenant/v2.0",
        client_id_env=client_id_env,
        groups=GroupResolutionConfig(
            mode="api",
            tenant_id_env=tenant_id_env,
            client_secret_env=client_secret_env,
        ),
    )


class TestGraphCredentials:
    @pytest.mark.parametrize(
        "case", GRAPH_CREDENTIALS_CASES, ids=[case["id"] for case in GRAPH_CREDENTIALS_CASES]
    )
    def test_matches_expected(self, monkeypatch: pytest.MonkeyPatch, case: GraphCredentialsCase):
        for name, value in case["env"].items():
            monkeypatch.setenv(name, value)
        credentials = graph_credentials(provider=case["provider"], secrets=None)
        assert credentials == case["expected_credentials"]


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestEntraAdapterConstructor:
    def test_stores_provider_reference(self):
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        assert adapter._provider is provider

    def test_provider_type_is_entra_id(self):
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        assert adapter._provider.type == "entra_id"

    def test_reads_env_var_names_from_provider(self):
        provider = _make_provider(
            client_id_env="MY_CLIENT_ID",
            tenant_id_env="MY_TENANT_ID",
            client_secret_env="MY_CLIENT_SECRET",
        )
        adapter = EntraAdapter(provider)
        assert adapter._provider.client_id_env == "MY_CLIENT_ID"
        assert adapter._provider.groups.tenant_id_env == "MY_TENANT_ID"
        assert adapter._provider.groups.client_secret_env == "MY_CLIENT_SECRET"

    def test_graph_base_constant(self):
        assert EntraAdapter.GRAPH_BASE == "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# _get_token — no credentials available
# ---------------------------------------------------------------------------


class TestGetTokenMissingCredentials:
    def test_returns_none_when_no_env_vars_set(self, monkeypatch):
        # Ensure none of the env vars are present
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_client_id_env_var_missing(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.setenv("ENTRA_CLIENT_SECRET", "test-secret")

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_tenant_id_env_var_missing(self, monkeypatch):
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.setenv("ENTRA_CLIENT_SECRET", "test-secret")

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_client_secret_env_var_missing(self, monkeypatch):
        monkeypatch.setenv("ENTRA_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("ENTRA_TENANT_ID", "test-tenant-id")
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None

    def test_returns_none_when_env_var_name_is_empty_string(self, monkeypatch):
        # Provider configured with empty env var names
        provider = OIDCProvider(
            type="entra_id",
            issuer="https://login.microsoftonline.com/test/v2.0",
            client_id_env="",
            groups=GroupResolutionConfig(
                mode="api",
                tenant_id_env="",
                client_secret_env="",
            ),
        )
        adapter = EntraAdapter(provider)
        result = adapter._get_token()
        assert result is None


# ---------------------------------------------------------------------------
# resolve_groups — unfriendly fallback when no credentials
# ---------------------------------------------------------------------------


class TestResolveGroupsNoCredentials:
    async def test_returns_dict_with_ids_as_keys_and_values(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.resolve_groups(["group-1", "group-2"])
        assert result == {"group-1": "group-1", "group-2": "group-2"}

    async def test_returns_empty_dict_for_empty_input(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.resolve_groups([])
        assert result == {}

    async def test_return_type_is_dict(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.resolve_groups(["any-group-id"])
        assert isinstance(result, dict)

    async def test_single_group_id_in_fallback(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.resolve_groups(["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])
        assert result == {
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        }


# ---------------------------------------------------------------------------
# resolve_user_groups (the >200 overage enrichment) — empty when no credentials
# ---------------------------------------------------------------------------


class TestResolveUserGroupsNoCredentials:
    async def test_returns_empty_list_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        # Fail-open: no token -> no groups (default deny), never raises.
        result = await adapter.resolve_user_groups("some-user-oid")
        assert result == []

    async def test_empty_directory_id_short_circuits(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        adapter = EntraAdapter(_make_provider())
        assert await adapter.resolve_user_groups("") == []


# ---------------------------------------------------------------------------
# list_all_groups — empty list when no credentials
# ---------------------------------------------------------------------------


class TestListAllGroupsNoCredentials:
    async def test_returns_empty_list_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.list_all_groups()
        assert result == []

    async def test_return_type_is_list(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.list_all_groups()
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# test_connection — returns (False, message) when no credentials
# ---------------------------------------------------------------------------


class TestTestConnectionNoCredentials:
    async def test_returns_false_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        success, message = await adapter.test_connection()
        assert success is False

    async def test_returns_message_string_when_no_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        success, message = await adapter.test_connection()
        assert isinstance(message, str)
        assert len(message) > 0

    async def test_returns_tuple_of_two_elements(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        result = await adapter.test_connection()
        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_message_mentions_credentials(self, monkeypatch):
        monkeypatch.delenv("ENTRA_CLIENT_ID", raising=False)
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_CLIENT_SECRET", raising=False)

        adapter = EntraAdapter(_make_provider())
        _success, message = await adapter.test_connection()
        # Message should explain why it failed — credentials issue
        assert any(
            keyword in message.lower()
            for keyword in ("credential", "token", "configure", "missing", "env")
        )


# ---------------------------------------------------------------------------
# Live Entra tests (skipped — require sandbox)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Needs Entra sandbox")
class TestEntraAdapterLive:
    async def test_resolve_groups_returns_display_names(self):
        """Verify group GUIDs are resolved to display names via Graph API."""
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        group_ids = [os.environ["TEST_ENTRA_GROUP_ID"]]
        result = await adapter.resolve_groups(group_ids)
        assert group_ids[0] in result
        assert result[group_ids[0]] != group_ids[0]

    async def test_list_all_groups_returns_group_info_objects(self):
        """Verify list_all_groups returns GroupInfo with id and name."""
        from dfe_engine.auth.oidc.models import GroupInfo

        provider = _make_provider()
        adapter = EntraAdapter(provider)
        groups = await adapter.list_all_groups()
        assert len(groups) > 0
        for g in groups:
            assert isinstance(g, GroupInfo)
            assert g.id
            assert g.name

    async def test_test_connection_returns_true_with_valid_credentials(self):
        """Verify connection test succeeds with valid Entra credentials."""
        provider = _make_provider()
        adapter = EntraAdapter(provider)
        success, message = await adapter.test_connection()
        assert success is True
        assert message
