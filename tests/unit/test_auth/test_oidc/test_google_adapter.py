#  Project:      DFE Engine
#  File:         tests/unit/test_auth/test_oidc/test_google_adapter.py
#  Purpose:      Unit tests for Google Workspace Admin SDK group adapter.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json
import os
import re

import pytest

from dfe_engine.auth.oidc.adapters.base import DirectoryError
from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from tests.unit.test_auth.factories import (
    make_directory_reply,
    make_google_adapter,
    make_google_directory_service,
    make_google_service_account_json,
    make_oidc_provider,
)
from tests.unit.test_auth.test_oidc.google_adapter_cases import (
    GOOGLE_ISSUER,
    LIST_ALL_GROUPS_CASES,
    LIST_ALL_GROUPS_MISSING_SERVICE_ACCOUNT_CASES,
    LIST_ALL_GROUPS_RAISES_CASES,
    TOKEN_PATH,
    TOKEN_REPLY,
    ListAllGroupsCase,
    ListAllGroupsMissingServiceAccountCase,
    ListAllGroupsRaisesCase,
)
from tests.unit.test_auth.test_oidc.local_directory import LocalDirectory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_listing_provider() -> OIDCProvider:
    """An api-mode Google provider for example.com whose service account JSON is read from DFE_TEST_GOOGLE_SA_JSON."""
    groups = {
        "admin_email": "admin@example.com",
        "domain": "example.com",
        "mode": "api",
        "service_account_json_env": "DFE_TEST_GOOGLE_SA_JSON",
    }
    return make_oidc_provider(groups=groups, issuer=GOOGLE_ISSUER, type="google")


def _make_provider(
    service_account_json_env: str | None = "DFE_GOOGLE_SA_JSON",
    admin_email: str | None = "admin@example.com",
    domain: str | None = "example.com",
) -> OIDCProvider:
    return OIDCProvider(
        type="google",
        display_name="Google Workspace",
        issuer="https://accounts.google.com",
        client_id_env="DFE_OIDC_GOOGLE_CLIENT_ID",
        groups=GroupResolutionConfig(
            service_account_json_env=service_account_json_env,
            admin_email=admin_email,
            domain=domain,
        ),
    )


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestGoogleAdapterConstructor:
    def test_stores_provider(self) -> None:
        provider = _make_provider()
        adapter = GoogleAdapter(provider)
        assert adapter._provider is provider

    def test_provider_name_accessible(self) -> None:
        provider = _make_provider()
        adapter = GoogleAdapter(provider)
        assert adapter._provider.type == "google"

    def test_groups_config_accessible(self) -> None:
        provider = _make_provider(domain="mycompany.com")
        adapter = GoogleAdapter(provider)
        assert adapter._provider.groups.domain == "mycompany.com"

    def test_no_service_account_env_set(self) -> None:
        # Provider with no env var name configured
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        assert adapter._provider.groups.service_account_json_env == ""


# ---------------------------------------------------------------------------
# _get_service — missing credentials
# ---------------------------------------------------------------------------


class TestGetServiceMissingCredentials:
    def test_returns_none_when_env_var_name_not_configured(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        service = adapter._get_service()
        assert service is None

    def test_returns_none_when_env_var_not_set(self) -> None:
        # Env var name is configured but the env var itself is absent
        provider = _make_provider(service_account_json_env="DFE_NONEXISTENT_SA_JSON_XYZ")
        adapter = GoogleAdapter(provider)
        # Ensure it's absent
        os.environ.pop("DFE_NONEXISTENT_SA_JSON_XYZ", None)
        service = adapter._get_service()
        assert service is None

    def test_returns_none_when_env_var_contains_invalid_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DFE_GOOGLE_SA_JSON", "not-valid-json")
        provider = _make_provider(service_account_json_env="DFE_GOOGLE_SA_JSON")
        adapter = GoogleAdapter(provider)
        service = adapter._get_service()
        assert service is None

    def test_returns_none_when_env_var_contains_wrong_json_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Valid JSON but not a service account dict
        monkeypatch.setenv("DFE_GOOGLE_SA_JSON", '["not", "a", "dict"]')
        provider = _make_provider(service_account_json_env="DFE_GOOGLE_SA_JSON")
        adapter = GoogleAdapter(provider)
        service = adapter._get_service()
        assert service is None


# ---------------------------------------------------------------------------
# resolve_groups — missing credentials (unfriendly fallback)
# ---------------------------------------------------------------------------


class TestResolveGroupsMissingCredentials:
    async def test_returns_identity_map_when_no_credentials(self) -> None:
        # Service account env var not configured — should return {id: id} fallback
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        result = await adapter.resolve_groups(["group-a@example.com", "group-b@example.com"])
        assert result == {
            "group-a@example.com": "group-a@example.com",
            "group-b@example.com": "group-b@example.com",
        }

    async def test_returns_empty_dict_for_empty_input(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        result = await adapter.resolve_groups([])
        assert result == {}

    async def test_returns_identity_map_when_env_var_missing(self) -> None:
        os.environ.pop("DFE_MISSING_SA_JSON_ABC", None)
        provider = _make_provider(service_account_json_env="DFE_MISSING_SA_JSON_ABC")
        adapter = GoogleAdapter(provider)
        result = await adapter.resolve_groups(["id1", "id2"])
        assert result == {"id1": "id1", "id2": "id2"}


# ---------------------------------------------------------------------------
# resolve_user_groups (login enrichment) — missing credentials
# ---------------------------------------------------------------------------


class TestResolveUserGroupsMissingCredentials:
    async def test_returns_empty_list_when_no_credentials(self) -> None:
        # Google login enrichment fails open to default deny, never raises.
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        result = await adapter.resolve_user_groups("user@example.com")
        assert result == []

    async def test_empty_user_key_short_circuits(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        assert await adapter.resolve_user_groups("") == []


class TestListAllGroups:
    @pytest.mark.parametrize(
        "case", LIST_ALL_GROUPS_CASES, ids=[case["id"] for case in LIST_ALL_GROUPS_CASES]
    )
    async def test_matches_expected(self, http_directory: LocalDirectory, case: ListAllGroupsCase):
        # The Admin SDK only answers at admin.googleapis.com, so a service pointed at the local directory is passed in.
        http_directory.replies = {TOKEN_PATH: TOKEN_REPLY, **case["replies"]}
        adapter = make_google_adapter(provider=_make_listing_provider())
        service = make_google_directory_service(api_endpoint=http_directory.base_url)
        assert await adapter._list_groups(service=service) == case["expected_groups"]

    @pytest.mark.parametrize(
        "case",
        LIST_ALL_GROUPS_MISSING_SERVICE_ACCOUNT_CASES,
        ids=[case["id"] for case in LIST_ALL_GROUPS_MISSING_SERVICE_ACCOUNT_CASES],
    )
    async def test_missing_service_account(
        self, monkeypatch: pytest.MonkeyPatch, case: ListAllGroupsMissingServiceAccountCase
    ):
        for name, value in case["env"].items():
            monkeypatch.setenv(name, value)
        with pytest.raises(DirectoryError, match=re.escape(case["message"])):
            await make_google_adapter(provider=case["provider"]).list_all_groups()

    @pytest.mark.parametrize(
        "case",
        LIST_ALL_GROUPS_RAISES_CASES,
        ids=[case["id"] for case in LIST_ALL_GROUPS_RAISES_CASES],
    )
    async def test_raises(self, http_directory: LocalDirectory, case: ListAllGroupsRaisesCase):
        # The Admin SDK only answers at admin.googleapis.com, so a service pointed at the local directory is passed in.
        http_directory.replies = {TOKEN_PATH: TOKEN_REPLY, **case["replies"]}
        adapter = make_google_adapter(provider=_make_listing_provider())
        service = make_google_directory_service(api_endpoint=http_directory.base_url)
        with pytest.raises(DirectoryError, match=re.escape(case["message"])):
            await adapter._list_groups(service=service)

    async def test_token_refused(
        self, http_directory: LocalDirectory, monkeypatch: pytest.MonkeyPatch
    ):
        body = {
            "error": "unauthorized_client",
            "error_description": "Client is unauthorized to retrieve access tokens using this method.",
        }
        http_directory.replies = {"/token": make_directory_reply(body=json.dumps(body), status=401)}
        token_uri = f"{http_directory.base_url}/token"
        monkeypatch.setenv(
            "DFE_TEST_GOOGLE_SA_JSON", make_google_service_account_json(token_uri=token_uri)
        )
        adapter = make_google_adapter(provider=_make_listing_provider())
        message = (
            "google directory: the service account token request failed: 'unauthorized_client: "
            "Client is unauthorized to retrieve access tokens using this method.'"
        )
        with pytest.raises(DirectoryError, match=re.escape(message)):
            await adapter.list_all_groups()

    async def test_unreachable(self, monkeypatch: pytest.MonkeyPatch):
        # Nothing listens on port 1, so the token request is refused before any HTTP.
        token_uri = "http://127.0.0.1:1/token"
        monkeypatch.setenv(
            "DFE_TEST_GOOGLE_SA_JSON", make_google_service_account_json(token_uri=token_uri)
        )
        adapter = make_google_adapter(provider=_make_listing_provider())
        message = "google directory: the groups request failed: ConnectionRefusedError"
        with pytest.raises(DirectoryError, match=re.escape(message)):
            await adapter.list_all_groups()


# ---------------------------------------------------------------------------
# test_connection — missing credentials
# ---------------------------------------------------------------------------


class TestConnectionMissingCredentials:
    async def test_returns_false_when_no_service_account_env_configured(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        success, message = await adapter.test_connection()
        assert success is False
        assert isinstance(message, str)
        assert len(message) > 0

    async def test_returns_false_when_env_var_not_present(self) -> None:
        os.environ.pop("DFE_MISSING_SA_JSON_GHI", None)
        provider = _make_provider(service_account_json_env="DFE_MISSING_SA_JSON_GHI")
        adapter = GoogleAdapter(provider)
        success, message = await adapter.test_connection()
        assert success is False
        assert isinstance(message, str)

    async def test_message_describes_failure(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        success, message = await adapter.test_connection()
        assert success is False
        # Message should hint at credentials/configuration, not a raw exception traceback
        assert (
            "credential" in message.lower()
            or "config" in message.lower()
            or "service account" in message.lower()
            or "not configured" in message.lower()
        )


# ---------------------------------------------------------------------------
# GroupInfo model used by adapter
# ---------------------------------------------------------------------------


class TestConnectionTokenRefused:
    async def test_reports_the_token_endpoint_summary(
        self, http_directory: LocalDirectory, monkeypatch: pytest.MonkeyPatch
    ):
        body = {
            "error": "unauthorized_client",
            "error_description": "Client is unauthorized to retrieve access tokens using this method.",
        }
        http_directory.replies = {"/token": make_directory_reply(body=json.dumps(body), status=401)}
        token_uri = f"{http_directory.base_url}/token"
        monkeypatch.setenv(
            "DFE_TEST_GOOGLE_SA_JSON", make_google_service_account_json(token_uri=token_uri)
        )
        adapter = make_google_adapter(provider=_make_listing_provider())
        message = (
            "Google Admin SDK connection failed: the service account token request failed: "
            "'unauthorized_client: Client is unauthorized to retrieve access tokens using this "
            "method.'"
        )
        assert await adapter.test_connection() == (False, message)


class TestGroupInfo:
    def test_required_fields(self) -> None:
        group = GroupInfo(id="12345", name="Engineering")
        assert group.id == "12345"
        assert group.name == "Engineering"
        assert group.email == ""

    def test_optional_email(self) -> None:
        group = GroupInfo(id="12345", name="Engineering", email="eng@example.com")
        assert group.email == "eng@example.com"


# ---------------------------------------------------------------------------
# Real API tests (skipped — require Google Workspace sandbox)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Needs Google Workspace sandbox — not mocking")
class TestGoogleAdapterRealAPI:
    """Integration tests that exercise the real Google Admin SDK.

    These require:
    - A valid service account JSON in DFE_TEST_GOOGLE_SA_JSON
    - Domain-wide delegation configured for admin.directory.group.readonly
    - DFE_TEST_GOOGLE_ADMIN_EMAIL set to a Workspace admin
    - DFE_TEST_GOOGLE_DOMAIN set to the target domain
    """

    async def test_list_groups_returns_results(self) -> None:
        provider = OIDCProvider(
            name="google",
            issuer="https://accounts.google.com",
            client_id="test",
            groups=GroupResolutionConfig(
                service_account_json_env="DFE_TEST_GOOGLE_SA_JSON",
                admin_email=os.environ.get("DFE_TEST_GOOGLE_ADMIN_EMAIL"),
                domain=os.environ.get("DFE_TEST_GOOGLE_DOMAIN"),
            ),
        )
        adapter = GoogleAdapter(provider)
        groups = await adapter.list_all_groups()
        assert isinstance(groups, list)
        # At minimum should not error — real assertion depends on workspace contents

    async def test_connection_succeeds(self) -> None:
        provider = OIDCProvider(
            name="google",
            issuer="https://accounts.google.com",
            client_id="test",
            groups=GroupResolutionConfig(
                service_account_json_env="DFE_TEST_GOOGLE_SA_JSON",
                admin_email=os.environ.get("DFE_TEST_GOOGLE_ADMIN_EMAIL"),
                domain=os.environ.get("DFE_TEST_GOOGLE_DOMAIN"),
            ),
        )
        adapter = GoogleAdapter(provider)
        success, message = await adapter.test_connection()
        assert success is True, f"Connection failed: {message}"
