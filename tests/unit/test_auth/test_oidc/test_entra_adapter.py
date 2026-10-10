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

import pytest

from dfe_engine.auth.oidc.adapters.entra import EntraAdapter, graph_credentials
from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from tests.unit.test_auth.test_oidc.entra_adapter_cases import (
    GRAPH_CREDENTIALS_CASES,
    GraphCredentialsCase,
)
from tests.unit.test_auth.test_oidc.local_graph import LocalGraph

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


# ---------------------------------------------------------------------------
# Overage: the user's own token first, then the app credentials
# ---------------------------------------------------------------------------

OID = "00000000-0000-4000-8000-0000000000aa"
ADMINS = {"id": "11111111-0000-4000-8000-000000000001", "displayName": "dfe-admins"}
VIEWERS = {"id": "11111111-0000-4000-8000-000000000002", "displayName": "dfe-viewers"}
USER_TOKEN = "user-token"
SCOPE_REFUSED = {
    "error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges"}
}


@pytest.fixture
def graph():
    server = LocalGraph()
    server.start()
    yield server
    server.stop()


def _overage_adapter(
    server: LocalGraph, *, token: str = USER_TOKEN, app_token: str | None = None
) -> EntraAdapter:
    """An Entra adapter reading Graph at *server*; *app_token* stands in for the client-credentials token."""
    adapter = EntraAdapter(_make_provider(), access_token=token)
    adapter.GRAPH_BASE = server.base_url
    adapter._get_token = lambda: app_token  # type: ignore[method-assign]
    return adapter


class TestOverageUserToken:
    async def test_the_users_own_token_reads_their_groups(self, graph):
        graph.pages["me"] = [[ADMINS, VIEWERS]]
        groups = await _overage_adapter(graph).resolve_user_groups(OID)

        assert [g.name for g in groups] == ["dfe-admins", "dfe-viewers"]
        assert graph.principals == ["me"]
        assert graph.requests[0]["authorization"] == "Bearer user-token"

    async def test_a_refused_user_token_falls_back_to_the_app_credentials(self, graph):
        graph.refuse["me"] = (403, SCOPE_REFUSED)
        graph.pages[f"users/{OID}"] = [[VIEWERS]]
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert [g.name for g in groups] == ["dfe-viewers"]
        assert graph.principals == ["me", f"users/{OID}"]
        assert graph.requests[1]["authorization"] == "Bearer app-token"

    async def test_no_groups_for_the_user_is_the_answer(self, graph):
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert groups == []
        assert graph.principals == ["me"]

    async def test_without_a_user_token_the_app_credentials_answer(self, graph):
        graph.pages[f"users/{OID}"] = [[ADMINS]]
        groups = await _overage_adapter(graph, token="", app_token="app-token").resolve_user_groups(
            OID
        )

        assert [g.name for g in groups] == ["dfe-admins"]
        assert graph.principals == [f"users/{OID}"]

    async def test_refused_everywhere_is_default_deny(self, graph):
        graph.refuse["me"] = (403, SCOPE_REFUSED)
        graph.refuse[f"users/{OID}"] = (403, SCOPE_REFUSED)
        assert await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID) == []

    async def test_every_page_is_read(self, graph):
        graph.pages["me"] = [[ADMINS], [VIEWERS]]
        groups = await _overage_adapter(graph).resolve_user_groups(OID)

        assert [g.name for g in groups] == ["dfe-admins", "dfe-viewers"]
        assert graph.principals == ["me", "me"]

    def test_the_factory_hands_entra_the_login_token(self):
        from dfe_engine.auth.oidc.adapters import get_adapter

        adapter = get_adapter(_make_provider(), access_token="login-token")
        assert isinstance(adapter, EntraAdapter)
        assert adapter._access_token == "login-token"

    async def test_groups_the_token_may_not_read_still_resolve_by_id(self, graph):
        """Graph answers a User.Read token with each group's id and every other property null."""
        graph.pages["me"] = [[ADMINS, VIEWERS]]
        graph.limited.add("me")
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert [g.id for g in groups] == [ADMINS["id"], VIEWERS["id"]]
        assert [g.name for g in groups] == ["", ""]
        assert graph.principals == ["me"]

    async def test_a_failure_partway_through_is_no_answer_not_a_partial_one(self, graph):
        graph.pages["me"] = [[ADMINS], [VIEWERS]]
        graph.fail_page["me"] = 1
        graph.pages[f"users/{OID}"] = [[ADMINS, VIEWERS]]
        groups = await _overage_adapter(graph, app_token="app-token").resolve_user_groups(OID)

        assert [g.name for g in groups] == ["dfe-admins", "dfe-viewers"]
        assert graph.principals == ["me", "me", f"users/{OID}"]
