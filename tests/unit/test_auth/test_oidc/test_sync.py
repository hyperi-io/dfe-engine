#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_sync.py
#  Purpose:      Tests for OIDC group sync runner
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import json
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.api.deps import _resolve_roles_from_groups
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GROUPS_SKIPPED, GroupMetrics, GroupStore
from dfe_engine.auth.membership import linked_groups
from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.mock import MockDirectoryAdapter
from dfe_engine.auth.oidc.adapters.okta import _group_info_from_okta
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import SYNC_GROUPS_SKIPPED, SyncMetrics, _safe_name, sync_provider
from tests.unit.test_auth.factories import make_directory_reply, make_okta_directory_provider
from tests.unit.test_auth.test_oidc.local_directory import LocalDirectory
from tests.unit.test_auth.test_oidc.sync_cases import (
    HONOURED_LINK_CASES,
    ID_TAKEN_CASES,
    NON_API_MODE_CASES,
    HonouredLinkCase,
    IdTakenCase,
    NonApiModeCase,
)

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

        asyncio.run(sync_provider("test-sso", provider_registry, group_store, adapter=adapter))

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


class TestSyncUpdatesTheGroupsLinkedToIt:
    """A stored group linked to this provider group is refreshed, and keeps its roles."""

    async def test_roles_kept_on_update(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        # Pre-create the group with roles assigned by an admin, linked to g1
        group_store.create(
            name="admins-example.com",
            roles=["admin", "data_analyst"],
            description="Old description",
        )
        group_store.update("admins-example.com", source_provider="test-sso", source_id="g1")

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
        assert result["groups_skipped"] == 0

        updated = group_store.get("admins-example.com")
        assert updated is not None
        # Roles preserved
        assert sorted(updated.roles) == ["admin", "data_analyst"]
        # Description updated from provider
        assert updated.description == "New description from provider"
        assert (updated.source_provider, updated.source_id) == ("test-sso", "g1")

    async def test_a_second_sync_updates_every_group_the_first_created(
        self, registries, api_provider
    ):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        groups = [
            GroupInfo(id="g1", name="Security Operators", email=""),
            GroupInfo(id="g2", name="ops", email="ops@example.com"),
        ]
        adapter = FakeAdapter(api_provider, groups)
        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        again = await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        assert (again["created"], again["updated"], again["groups_skipped"]) == (0, 2, 0)
        assert provider_registry.get("test-sso").last_sync_status == "ok"


class TestSyncHonoursALinkAnAdminMade:
    """A stored group carrying a provider group's id is that group, whatever name the directory gives it."""

    @pytest.mark.parametrize(
        "case", HONOURED_LINK_CASES, ids=[case["id"] for case in HONOURED_LINK_CASES]
    )
    async def test_it_is_updated_and_no_second_group_is_created(
        self,
        api_provider: OIDCProvider,
        registries: tuple[OIDCProviderRegistry, GroupStore],
        case: HonouredLinkCase,
    ):
        provider_registry, group_store = registries
        provider_registry.create(name="test-sso", provider=api_provider)
        group_store.create(description="Ours", name="dfe-viewers", roles=["data_viewer"])
        group_store.update(
            name="dfe-viewers", source_id="00g-viewers", source_provider=case["source_provider"]
        )
        groups = [
            GroupInfo(description="Okta viewers", email="", id="00g-viewers", name="Okta Viewers")
        ]

        result = await sync_provider(
            adapter=FakeAdapter(groups=groups, provider=api_provider),
            bindings=case["bindings"],
            group_store=group_store,
            provider_name="test-sso",
            provider_registry=provider_registry,
        )

        assert (result["created"], result["updated"], result["groups_skipped"]) == (0, 1, 0)
        stored = [
            (group.name, group.description, group.roles, group.source_provider)
            for group in group_store.list()
        ]
        assert stored == [
            ("dfe-viewers", "Okta viewers", ["data_viewer"], case["expected_source_provider"])
        ]

    @pytest.mark.parametrize("case", ID_TAKEN_CASES, ids=[case["id"] for case in ID_TAKEN_CASES])
    async def test_one_linked_to_another_provider_is_skipped_as_id_taken(
        self,
        api_provider: OIDCProvider,
        registries: tuple[OIDCProviderRegistry, GroupStore],
        case: IdTakenCase,
    ):
        provider_registry, group_store = registries
        provider_registry.create(name="test-sso", provider=api_provider)
        group_store.create(description="Ours", name="dfe-viewers", roles=["data_viewer"])
        group_store.update(
            name="dfe-viewers", source_id="00g-viewers", source_provider=case["source_provider"]
        )
        groups = [
            GroupInfo(description="Okta viewers", email="", id="00g-viewers", name="Okta Viewers")
        ]
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)

        result = await sync_provider(
            adapter=FakeAdapter(groups=groups, provider=api_provider),
            bindings=case["bindings"],
            group_store=group_store,
            metrics=SyncMetrics(manager),
            provider_name="test-sso",
            provider_registry=provider_registry,
        )

        assert (result["created"], result["updated"], result["groups_skipped"]) == (0, 0, 1)
        assert [(group.name, group.description) for group in group_store.list()] == [
            ("dfe-viewers", "Ours")
        ]
        assert _sync_skips(manager) == {"id_taken": 1.0}
        status = "partial: 1 of 1 groups skipped, their id is held by a group linked to a provider not bound to this one"
        assert [
            (name, provider.last_sync_status) for name, provider in provider_registry.list()
        ] == [("test-sso", status)]


class TestSyncOverAHolderItCannotUpdate:
    """A stored group carrying a provider group's id under a name no group can have is skipped while the rest sync."""

    async def test_it_is_skipped_counted_and_the_rest_sync(
        self,
        api_provider: OIDCProvider,
        registries: tuple[OIDCProviderRegistry, GroupStore],
        tmp_path: Path,
    ):
        provider_registry, group_store = registries
        provider_registry.create(name="test-sso", provider=api_provider)
        legacy = tmp_path / "groups" / "_legacy-admins.yaml"
        legacy.write_text("roles: [admin]\nsource_id: g1\n", encoding="utf-8")
        groups = [
            GroupInfo(email="", id="g0", name="Zero"),
            GroupInfo(email="", id="g1", name="One"),
            GroupInfo(email="", id="g2", name="Two"),
        ]
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)

        result = await sync_provider(
            adapter=FakeAdapter(groups=groups, provider=api_provider),
            group_store=group_store,
            metrics=SyncMetrics(manager),
            provider_name="test-sso",
            provider_registry=provider_registry,
        )

        assert (result["created"], result["updated"], result["groups_skipped"]) == (2, 0, 1)
        assert [group.name for group in group_store.list()] == ["_legacy-admins", "two", "zero"]
        assert _sync_skips(manager) == {"holder_unwritable": 1.0}
        status = "partial: 1 of 3 groups skipped, their id is held by a stored group the sync cannot update"
        assert [
            (name, provider.last_sync_status) for name, provider in provider_registry.list()
        ] == [("test-sso", status)]


class TestSyncNeverTakesAGroupNoAdminLinked:
    """An IdP group reaches a stored group only through a link an admin made, never its name.

    A display name is not unique in a directory, and in a tenant that lets users
    create groups anyone can pick one, so a name match is no evidence of identity.
    """

    ATTACKER = "attacker-guid-123"

    @pytest.fixture
    def seeded(self, tmp_path, api_provider):
        """The boot seed's group store, dfe-admins carrying admin, and a provider in api mode."""
        _, group_store, *_ = bootstrap_auth(tmp_path / "auth")
        provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider_registry.create("test-sso", api_provider)
        return provider_registry, group_store

    @staticmethod
    def _entra(provider, tmp_path, monkeypatch, attacker):
        # An Entra security group has no mail, so its displayName names it.
        return FakeAdapter(provider, [GroupInfo(id=attacker, name="DFE Admins", email="")])

    @staticmethod
    def _okta(provider, tmp_path, monkeypatch, attacker):
        item = {"id": attacker, "profile": {"name": "DFE Admins", "description": ""}}
        return FakeAdapter(provider, [_group_info_from_okta(item)])

    @staticmethod
    def _mock_directory(provider, tmp_path, monkeypatch, attacker):
        fixture = tmp_path / "directory.json"
        group = {"id": attacker, "name": "DFE Admins", "email": ""}
        fixture.write_text(json.dumps({"groups": [group]}), encoding="utf-8")
        monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(fixture))
        return MockDirectoryAdapter(provider)

    @pytest.mark.parametrize("directory", ["_entra", "_okta", "_mock_directory"])
    async def test_a_group_named_like_the_seeded_admins_is_skipped(
        self, seeded, api_provider, tmp_path, monkeypatch, directory
    ):
        provider_registry, group_store = seeded
        before = group_store.get("dfe-admins")
        adapter = getattr(self, directory)(api_provider, tmp_path, monkeypatch, self.ATTACKER)
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)

        result = await sync_provider(
            "test-sso",
            provider_registry,
            group_store,
            adapter=adapter,
            metrics=SyncMetrics(manager),
        )

        assert (result["created"], result["updated"], result["groups_skipped"]) == (0, 0, 1)
        assert group_store.get("dfe-admins") == before
        assert before.source_provider == ""
        assert before.source_id == ""
        assert _resolve_roles_from_groups([self.ATTACKER], group_store) == ([], [])
        assert _sync_skips(manager) == {"name_taken": 1.0}
        assert provider_registry.get("test-sso").last_sync_status == (
            "partial: 1 of 1 groups skipped, their name is held by a group not linked to them"
        )

    async def test_a_group_keyed_on_its_email_is_skipped_the_same_way(
        self, registries, api_provider
    ):
        """A Google group always has an email, so its email slug is the name it collides on."""
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        group_store.create("soc-example.com", roles=["data_analyst"], description="Ours")
        groups = [GroupInfo(id=self.ATTACKER, name="SOC", email="soc@example.com")]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        assert result["groups_skipped"] == 1
        stored = group_store.get("soc-example.com")
        assert (stored.source_provider, stored.source_id, stored.description) == ("", "", "Ours")
        assert _resolve_roles_from_groups([self.ATTACKER], group_store) == ([], [])

    async def test_a_group_linked_to_another_idp_group_keeps_its_link(
        self, registries, api_provider
    ):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        group_store.create("soc-team", roles=["data_analyst"])
        group_store.update("soc-team", source_provider="test-sso", source_id="g-soc")
        groups = [GroupInfo(id=self.ATTACKER, name="SOC Team", email="")]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        assert (result["updated"], result["groups_skipped"]) == (0, 1)
        assert group_store.get("soc-team").source_id == "g-soc"
        assert _resolve_roles_from_groups([self.ATTACKER], group_store) == ([], [])
        linked = linked_groups(
            groups=group_store.list(), identifiers=["g-soc"], providers={"test-sso"}
        )
        assert [group.name for group in linked] == ["soc-team"]

    async def test_a_group_linked_by_id_alone_syncs_and_gains_its_provider(
        self, registries, api_provider
    ):
        """SCIM and a hand-edited group file set only source_id, and that id is the link."""
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        group_store.create("soc-team", roles=["data_analyst"], description="Old")
        group_store.update("soc-team", source_id="g-soc")
        groups = [GroupInfo(id="g-soc", name="SOC Team", email="", description="New")]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        assert (result["updated"], result["groups_skipped"]) == (1, 0)
        stored = group_store.get("soc-team")
        assert (stored.source_provider, stored.source_id) == ("test-sso", "g-soc")
        assert (stored.roles, stored.description) == (["data_analyst"], "New")

    async def test_an_idp_group_with_no_id_links_nothing(self, seeded, api_provider):
        """An empty id would otherwise match every group that carries no source_id."""
        provider_registry, group_store = seeded
        before = group_store.get("dfe-admins")
        groups = [GroupInfo(id="", name="DFE Admins", email="")]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        assert (result["updated"], result["groups_skipped"]) == (0, 1)
        assert group_store.get("dfe-admins") == before

    async def test_a_skipped_group_does_not_stop_the_rest(self, seeded, api_provider):
        provider_registry, group_store = seeded
        groups = [
            GroupInfo(id=self.ATTACKER, name="DFE Admins", email=""),
            GroupInfo(id="g-new", name="Threat Hunters", email=""),
        ]
        warnings: list[str] = []
        handler = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
        try:
            result = await sync_provider(
                "test-sso",
                provider_registry,
                group_store,
                adapter=FakeAdapter(api_provider, groups),
            )
        finally:
            logger.remove(handler)

        assert (result["created"], result["groups_skipped"]) == (1, 1)
        created = group_store.get("threat-hunters")
        assert (created.source_provider, created.source_id) == ("test-sso", "g-new")
        assert (
            "OIDC group sync skipped a group whose name is held by a group not linked to it"
            in warnings
        )


class TestSyncOverAGroupFileThatDoesNotLoad:
    """One unloadable file holding a synced group's name must not abort the whole sync."""

    _BAD_SCOPE = "roles: [admin]\nscope: org:../elsewhere/outside\n"

    async def test_that_group_is_skipped_and_the_rest_sync(self, tmp_path, api_provider):
        provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider_registry.create("test-sso", api_provider)
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        group_store = GroupStore(tmp_path / "groups", metrics=GroupMetrics(manager))
        stored = tmp_path / "groups" / "admins-example.com.yaml"
        stored.write_text(self._BAD_SCOPE, encoding="utf-8")
        groups = [
            GroupInfo(id="g1", name="admins", email="admins@example.com"),
            GroupInfo(id="g2", name="operators", email="ops@example.com"),
        ]
        warnings: list[str] = []
        handler = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
        try:
            result = await sync_provider(
                "test-sso",
                provider_registry,
                group_store,
                adapter=FakeAdapter(api_provider, groups),
                metrics=SyncMetrics(manager),
            )
        finally:
            logger.remove(handler)

        assert result["error"] is None
        assert result["created"] == 1
        assert group_store.get("ops-example.com").source_id == "g2"
        assert stored.read_text(encoding="utf-8") == self._BAD_SCOPE
        assert "OIDC group sync skipped a group whose stored copy does not load" in warnings
        assert result["groups_skipped"] == 1
        assert provider_registry.get("test-sso").last_sync_status == (
            "partial: 1 of 2 groups skipped, their stored copy does not load"
        )
        skipped = [
            s.value
            for f in text_string_to_metric_families(manager.metrics_text)
            for s in f.samples
            if s.name == GROUPS_SKIPPED
        ]
        assert skipped == [1.0]
        assert _sync_skips(manager) == {"stored_unloadable": 1.0}


def _sync_skips(manager) -> dict[str, float]:
    """The sync's skip counter, by reason, as the engine's /metrics serves it."""
    return {
        s.labels["reason"]: s.value
        for f in text_string_to_metric_families(manager.metrics_text)
        for s in f.samples
        if s.name == SYNC_GROUPS_SKIPPED
    }


class TestSyncOverAGroupWhoseIdentifierMakesNoName:
    """One provider group that sanitises to no valid group name must not abort the sync."""

    @pytest.mark.parametrize(
        "identifier",
        ["!!!", "a" * 129, "_team", ".."],
        ids=["empty", "too-long", "leading-underscore", "dots"],
    )
    async def test_it_is_skipped_counted_and_the_rest_sync(
        self, registries, api_provider, identifier
    ):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [
            GroupInfo(id="g-bad", name=identifier, email=""),
            GroupInfo(id="g2", name="operators", email="ops@example.com"),
        ]

        result = await sync_provider(
            "test-sso",
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, groups),
            metrics=SyncMetrics(manager),
        )

        assert result["error"] is None
        assert result["created"] == 1
        assert result["groups_skipped"] == 1
        assert [g.name for g in group_store.list()] == ["ops-example.com"]
        assert group_store.get("ops-example.com").source_id == "g2"
        assert provider_registry.get("test-sso").last_sync_status == (
            "partial: 1 of 2 groups skipped, their identifier makes no valid group name"
        )
        assert _sync_skips(manager) == {"invalid_name": 1.0}


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

        assert result["skipped"] == "The provider is disabled"
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
    @pytest.mark.parametrize(
        "case", NON_API_MODE_CASES, ids=[case["id"] for case in NON_API_MODE_CASES]
    )
    async def test_non_api_mode_returns_skipped(
        self, registries: tuple[OIDCProviderRegistry, GroupStore], case: NonApiModeCase
    ):
        provider_registry, group_store = registries
        provider_registry.create(name="sso", provider=case["provider"])

        result = await sync_provider(
            group_store=group_store, provider_name="sso", provider_registry=provider_registry
        )

        assert result == case["expected_result"]

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


class TestSyncReportsADirectoryFailure:
    @pytest.mark.usefixtures("okta_api_token")
    async def test_refused_listing(
        self, registries: tuple[OIDCProviderRegistry, GroupStore], tls_directory: LocalDirectory
    ):
        provider_registry, group_store = registries
        tls_directory.replies = {
            "/api/v1/groups?limit=200": make_directory_reply(
                body=json.dumps({"errorSummary": "Invalid token provided"}), status=401
            )
        }
        provider_registry.create(
            name="okta-dir", provider=make_okta_directory_provider(okta_domain=tls_directory.host)
        )

        result = await sync_provider(
            group_store=group_store, provider_name="okta-dir", provider_registry=provider_registry
        )

        error = "okta directory: GET '/api/v1/groups' returned HTTP 401: 'Invalid token provided'"
        stored = provider_registry.get(name="okta-dir")
        assert (result, stored.last_sync_status, stored.sync_error, group_store.list()) == (
            {
                "created": 0,
                "error": error,
                "groups_skipped": 0,
                "skipped": None,
                "total": 0,
                "updated": 0,
            },
            "error",
            error,
            [],
        )


class TestSyncHandlesAdapterError:
    async def test_adapter_error_returns_error_dict(
        self, api_provider: OIDCProvider, log_lines: list[str], registries
    ):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=error_adapter
        )

        assert result["error"] == "generic directory: RuntimeError"
        assert result["skipped"] is None
        assert result["created"] == 0
        assert result["total"] == 0
        # An unexpected error's own text could quote a credential, so only its type and frames are logged.
        leaked = [
            line
            for line in log_lines
            if ("RuntimeError: connection refused" in line)
            or ("'error': 'connection refused'" in line)
        ]
        assert leaked == []

    async def test_adapter_error_sets_provider_sync_error(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        await sync_provider("test-sso", provider_registry, group_store, adapter=error_adapter)

        updated_provider = provider_registry.get("test-sso")
        assert updated_provider is not None
        assert updated_provider.last_sync_status == "error"
        assert updated_provider.sync_error == "generic directory: RuntimeError"
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
