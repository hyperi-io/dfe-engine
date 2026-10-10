#  Project:      DFE Engine
#  File:         tests/unit/test_auth/test_oidc/test_google_adapter.py
#  Purpose:      Unit tests for Google Workspace Admin SDK group adapter.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import os

import pytest

from dfe_engine.auth.oidc.adapters.google import GoogleAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# list_all_groups — missing credentials
# ---------------------------------------------------------------------------


class TestListAllGroupsMissingCredentials:
    async def test_returns_empty_list_when_no_credentials(self) -> None:
        provider = _make_provider(service_account_json_env="")
        adapter = GoogleAdapter(provider)
        result = await adapter.list_all_groups()
        assert result == []

    async def test_returns_empty_list_when_env_var_missing(self) -> None:
        os.environ.pop("DFE_MISSING_SA_JSON_DEF", None)
        provider = _make_provider(service_account_json_env="DFE_MISSING_SA_JSON_DEF")
        adapter = GoogleAdapter(provider)
        result = await adapter.list_all_groups()
        assert isinstance(result, list)
        assert result == []


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
# Live Directory API (runs only when a delegated service account is supplied)
# ---------------------------------------------------------------------------

# The service account JSON itself, which the adapter reads from this env var.
_LIVE_SA_ENV = "DFE_GOOGLE_SA_JSON"
_LIVE_ADMIN_ENV = "DFE_GOOGLE_GROUPS_ADMIN_EMAIL"
# Defaults to the delegated admin, the one account certain to exist in the directory.
_LIVE_USER_ENV = "DFE_OIDC_GOOGLE_FIXTURE_USER"


def _live_provider() -> OIDCProvider:
    return OIDCProvider(
        type="google",
        issuer="https://accounts.google.com",
        groups=GroupResolutionConfig(
            admin_email=os.environ.get(_LIVE_ADMIN_ENV, ""),
            enrich_on_login=True,
            mode="api",
            service_account_json_env=_LIVE_SA_ENV,
        ),
    )


@pytest.mark.live
@pytest.mark.skipif(
    not (os.environ.get(_LIVE_SA_ENV) and os.environ.get(_LIVE_ADMIN_ENV)),
    reason=(
        f"Needs Google Workspace sandbox: set {_LIVE_SA_ENV} to a service account JSON "
        f"delegated admin.directory.group.readonly, and {_LIVE_ADMIN_ENV} to the admin it acts as"
    ),
)
class TestGoogleAdapterLiveDirectory:
    """The login-time group lookup against a real Workspace directory, never a mock."""

    async def test_connection_succeeds(self) -> None:
        success, message = await GoogleAdapter(_live_provider()).test_connection()
        assert success is True, message

    async def test_resolves_the_fixture_users_groups(self) -> None:
        user = os.environ.get(_LIVE_USER_ENV) or os.environ.get(_LIVE_ADMIN_ENV, "")
        groups = await GoogleAdapter(_live_provider()).resolve_user_groups(user)
        # The adapter fails open to [], so an empty answer is either no membership or a failed lookup.
        assert groups, "the fixture user resolved to no groups"
        assert all(group.id for group in groups)
