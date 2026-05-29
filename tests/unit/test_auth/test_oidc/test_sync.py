#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_sync.py
#  Purpose:      Tests for OIDC group sync runner
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import _safe_name, sync_provider

# ---------------------------------------------------------------------------
# Fake adapter — dependency injection, not mocking
# ---------------------------------------------------------------------------


class FakeAdapter(OIDCGroupAdapter):
    """Concrete adapter that returns a fixed list of GroupInfo for testing."""

    def __init__(self, provider: OIDCProvider, groups: list[GroupInfo]) -> None:
        super().__init__(provider)
        self._groups = groups

    async def resolve_groups(self, subject: str) -> list[GroupInfo]:
        return []

    async def list_all_groups(self) -> list[GroupInfo]:
        return self._groups

    async def test_connection(self) -> tuple[bool, str]:
        return (True, "fake")


class ErrorAdapter(OIDCGroupAdapter):
    """Adapter that always raises on list_all_groups."""

    def __init__(self, provider: OIDCProvider) -> None:
        super().__init__(provider)

    async def resolve_groups(self, subject: str) -> list[GroupInfo]:
        return []

    async def list_all_groups(self) -> list[GroupInfo]:
        raise RuntimeError("connection refused")

    async def test_connection(self) -> tuple[bool, str]:
        return (False, "connection refused")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_provider() -> OIDCProvider:
    return OIDCProvider(
        type="generic",
        enabled=True,
        display_name="Test SSO",
        issuer="https://sso.example.com",
        groups=GroupResolutionConfig(mode="api"),
    )


@pytest.fixture
def registries(tmp_path):
    """Return (OIDCProviderRegistry, GroupStore) backed by tmp_path."""
    provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
    group_store = GroupStore(tmp_path / "groups")
    return provider_registry, group_store


# ---------------------------------------------------------------------------
# _safe_name unit tests
# ---------------------------------------------------------------------------


class TestSafeName:
    def test_email_becomes_slug(self):
        assert _safe_name("devs@example.com") == "devs@example.com".lower().replace(
            "@", "-"
        ).replace(".", ".")
        # More explicit: @ becomes -, dots preserved
        result = _safe_name("devs@example.com")
        assert "@" not in result
        assert result == "devs-example.com"

    def test_spaces_become_hyphens(self):
        assert _safe_name("my group name") == "my-group-name"

    def test_consecutive_hyphens_collapsed(self):
        assert _safe_name("a  b") == "a-b"

    def test_leading_trailing_hyphens_stripped(self):
        assert _safe_name("  spaces  ") == "spaces"

    def test_already_safe_unchanged(self):
        assert _safe_name("admins") == "admins"

    def test_uppercase_lowercased(self):
        assert _safe_name("ADMINS") == "admins"


# ---------------------------------------------------------------------------
# sync_provider tests
# ---------------------------------------------------------------------------


class TestSyncCreatesNewGroups:
    async def test_creates_group_files(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        groups = [
            GroupInfo(
                id="g1", name="admins", email="admins@example.com", description="Admin group"
            ),
            GroupInfo(id="g2", name="operators", email="", description="Ops team"),
        ]
        adapter = FakeAdapter(api_provider, groups)

        result = await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        assert result["created"] == 2
        assert result["updated"] == 0
        assert result["total"] == 2
        assert result["error"] is None
        assert result["skipped"] is None

    def test_group_files_have_source_provider(self, registries, api_provider):
        import asyncio

        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        groups = [GroupInfo(id="g1", name="admins", email="admins@example.com")]
        adapter = FakeAdapter(api_provider, groups)

        asyncio.get_event_loop().run_until_complete(
            sync_provider("test-sso", provider_registry, group_store, adapter=adapter)
        )

        # Use email as key: admins@example.com -> admins-example.com
        created = group_store.get("admins-example.com")
        assert created is not None
        assert created.source_provider == "test-sso"
        assert created.source_id == "g1"

    async def test_uses_name_when_no_email(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        groups = [GroupInfo(id="g1", name="My Operators", email="")]
        adapter = FakeAdapter(api_provider, groups)

        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        created = group_store.get("my-operators")
        assert created is not None
        assert created.source_id == "g1"

    async def test_uses_id_when_no_email_or_name(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        groups = [GroupInfo(id="group-abc-123", name="", email="")]
        adapter = FakeAdapter(api_provider, groups)

        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        created = group_store.get("group-abc-123")
        assert created is not None

    async def test_new_groups_have_empty_roles(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        groups = [GroupInfo(id="g1", name="devs", email="devs@example.com")]
        adapter = FakeAdapter(api_provider, groups)

        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        created = group_store.get("devs-example.com")
        assert created is not None
        assert created.roles == []


class TestSyncPreservesExistingRoles:
    async def test_roles_kept_on_update(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        # Pre-create the group with roles assigned by an admin
        group_store.create(
            name="admins-example.com",
            roles=["admin", "data_analyst"],
            description="Old description",
        )

        groups = [
            GroupInfo(
                id="g1",
                name="admins",
                email="admins@example.com",
                description="New description from provider",
            )
        ]
        adapter = FakeAdapter(api_provider, groups)

        result = await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        assert result["created"] == 0
        assert result["updated"] == 1

        updated = group_store.get("admins-example.com")
        assert updated is not None
        # Roles preserved
        assert sorted(updated.roles) == ["admin", "data_analyst"]
        # Description updated from provider
        assert updated.description == "New description from provider"

    async def test_source_metadata_updated_on_existing_group(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        group_store.create(name="ops-example.com", roles=["infra_viewer"])

        groups = [GroupInfo(id="new-id-456", name="ops", email="ops@example.com")]
        adapter = FakeAdapter(api_provider, groups)

        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        updated = group_store.get("ops-example.com")
        assert updated is not None
        assert updated.source_provider == "test-sso"
        assert updated.source_id == "new-id-456"
        assert updated.roles == ["infra_viewer"]


class TestSyncUpdatesProviderStatus:
    async def test_last_sync_at_set_after_success(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        adapter = FakeAdapter(api_provider, [])
        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        updated_provider = provider_registry.get("test-sso")
        assert updated_provider is not None
        assert updated_provider.last_sync_at != ""
        assert updated_provider.last_sync_status == "ok"

    async def test_last_sync_at_is_iso8601_utc(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        adapter = FakeAdapter(api_provider, [])
        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        updated_provider = provider_registry.get("test-sso")
        assert updated_provider is not None
        # ISO 8601 UTC timestamps contain 'T' and end with '+00:00' or 'Z'
        ts = updated_provider.last_sync_at
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")

    async def test_sync_error_cleared_on_success(self, registries, api_provider):
        provider_registry, group_store = registries
        provider = api_provider.model_copy(update={"sync_error": "previous error"})
        provider_registry.create("test-sso", provider)

        adapter = FakeAdapter(provider, [])
        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        updated_provider = provider_registry.get("test-sso")
        assert updated_provider is not None
        assert updated_provider.sync_error == ""


class TestSyncSkipsDisabledProvider:
    async def test_disabled_returns_skipped(self, registries):
        provider_registry, group_store = registries
        disabled = OIDCProvider(
            type="generic",
            enabled=False,
            groups=GroupResolutionConfig(mode="api"),
        )
        provider_registry.create("disabled-sso", disabled)

        result = await sync_provider("disabled-sso", provider_registry, group_store)

        assert result["skipped"] == "disabled"
        assert result["error"] is None
        assert result["created"] == 0

    async def test_disabled_does_not_create_groups(self, registries):
        provider_registry, group_store = registries
        disabled = OIDCProvider(
            type="generic",
            enabled=False,
            groups=GroupResolutionConfig(mode="api"),
        )
        provider_registry.create("disabled-sso", disabled)

        await sync_provider("disabled-sso", provider_registry, group_store)

        assert group_store.list() == []


class TestSyncSkipsNonApiMode:
    @pytest.mark.parametrize("mode", ["manual", "token_claim"])
    async def test_non_api_mode_returns_skipped(self, registries, mode):
        provider_registry, group_store = registries
        provider = OIDCProvider(
            type="generic",
            enabled=True,
            groups=GroupResolutionConfig(mode=mode),
        )
        provider_registry.create("sso", provider)

        result = await sync_provider("sso", provider_registry, group_store)

        assert result["skipped"] == f"mode is '{mode}'"
        assert result["error"] is None
        assert result["created"] == 0

    async def test_non_api_mode_does_not_create_groups(self, registries):
        provider_registry, group_store = registries
        provider = OIDCProvider(
            type="generic",
            enabled=True,
            groups=GroupResolutionConfig(mode="manual"),
        )
        provider_registry.create("sso", provider)

        await sync_provider("sso", provider_registry, group_store)

        assert group_store.list() == []


class TestSyncHandlesAdapterError:
    async def test_adapter_error_returns_error_dict(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=error_adapter
        )

        assert result["error"] == "connection refused"
        assert result["skipped"] is None
        assert result["created"] == 0
        assert result["total"] == 0

    async def test_adapter_error_sets_provider_sync_error(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        await sync_provider("test-sso", provider_registry, group_store, adapter=error_adapter)

        updated_provider = provider_registry.get("test-sso")
        assert updated_provider is not None
        assert updated_provider.last_sync_status == "error"
        assert updated_provider.sync_error == "connection refused"
        assert updated_provider.last_sync_at != ""

    async def test_adapter_error_does_not_create_groups(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        await sync_provider("test-sso", provider_registry, group_store, adapter=error_adapter)

        assert group_store.list() == []


class TestSyncUnknownProvider:
    async def test_unknown_provider_returns_error(self, registries):
        provider_registry, group_store = registries

        result = await sync_provider("nonexistent", provider_registry, group_store)

        assert "nonexistent" in result["error"]
        assert result["skipped"] is None
        assert result["created"] == 0

    async def test_unknown_provider_does_not_create_groups(self, registries):
        provider_registry, group_store = registries

        await sync_provider("nonexistent", provider_registry, group_store)

        assert group_store.list() == []


class TestSyncEmptyGroupList:
    async def test_empty_provider_returns_zero_counts(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        adapter = FakeAdapter(api_provider, [])
        result = await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        assert result["created"] == 0
        assert result["updated"] == 0
        assert result["total"] == 0
        assert result["error"] is None
        assert result["skipped"] is None
