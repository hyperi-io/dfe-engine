#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_sync.py
#  Purpose:      Tests for OIDC group sync runner
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import json

import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.logger import logger
from scalo.metrics import create_metrics

from dfe_engine.api.deps import _resolve_roles_from_groups
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GROUPS_SKIPPED, GroupMetrics, GroupStore
from dfe_engine.auth.membership import linked_groups, linked_providers
from dfe_engine.auth.oidc.adapters.base import OIDCGroupAdapter
from dfe_engine.auth.oidc.adapters.mock import MockDirectoryAdapter
from dfe_engine.auth.oidc.adapters.okta import _group_info_from_okta
from dfe_engine.auth.oidc.models import GroupInfo, GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.sync import (
    NO_DIRECTORY_MESSAGE,
    NOT_CONFIGURED_MESSAGE,
    STORE_FAILURE_MESSAGE,
    SYNC_GROUPS_SKIPPED,
    SyncMetrics,
    _safe_name,
    sync_provider,
)
from dfe_engine.auth.scim_mapping import SCIM_SOURCE_PROVIDER
from tests.support.failing_stores import CreateFailingGroupStore
from tests.unit.test_auth.test_oidc.refusing_store import PolicyRefusingSecretStore
from tests.unit.test_auth.test_oidc.sync_cases import (
    SYNC_IDENTIFIER_COLLISION_CASES,
    SyncIdentifierCollisionCase,
)
from tests.unit.test_auth.test_oidc.unreachable_store import UnreachableSecretStore

# ---------------------------------------------------------------------------
# Fake adapter — dependency injection, not mocking
# ---------------------------------------------------------------------------


class FakeAdapter(OIDCGroupAdapter):
    """Concrete adapter that returns a fixed list of GroupInfo for testing."""

    def __init__(self, provider: OIDCProvider, groups: list[GroupInfo]) -> None:
        super().__init__(provider)
        self._groups = groups

    async def list_all_groups(self) -> list[GroupInfo]:
        return self._groups

    async def test_connection(self) -> tuple[bool, str]:
        return (True, "fake")


class ErrorAdapter(OIDCGroupAdapter):
    """Adapter that always raises on list_all_groups."""

    def __init__(self, provider: OIDCProvider) -> None:
        super().__init__(provider)

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
        assert result["status"] == "ok"

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

    @pytest.mark.parametrize(
        ("stamp", "bindings"),
        [
            pytest.param("test-sso", {}, id="own"),
            pytest.param(SCIM_SOURCE_PROVIDER, {SCIM_SOURCE_PROVIDER: "test-sso"}, id="bound"),
        ],
    )
    async def test_a_renamed_directory_group_updates_the_group_linked_to_it(
        self, registries, api_provider, stamp, bindings
    ):
        """A group with no email is keyed on its name, so a rename must not make a second group."""
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        group_store.create(
            "soc-team",
            roles=["data_analyst"],
            description="Old",
            source_provider=stamp,
            source_id="g-soc",
        )
        renamed = [
            GroupInfo(id="g-soc", name="Security Operations", email="", description="Renamed")
        ]

        result = await sync_provider(
            "test-sso",
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, renamed),
            bindings=bindings,
        )

        assert (result["created"], result["updated"], result["groups_skipped"]) == (0, 1, 0)
        assert group_store.get("security-operations") is None
        stored = group_store.get("soc-team")
        assert (stored.source_provider, stored.source_id) == (stamp, "g-soc")
        assert (stored.roles, stored.description) == (["data_analyst"], "Renamed")
        providers = linked_providers(["test-sso"], bindings)
        linked = linked_groups(["g-soc"], group_store.list(), providers)
        assert [group.name for group in linked] == ["soc-team"]


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
        assert _resolve_roles_from_groups(["g-soc"], group_store) == (["data_analyst"], [])

    async def test_a_group_an_admin_linked_syncs_and_keeps_its_roles(
        self, registries, api_provider
    ):
        """An admin links a stored group by naming the provider and that provider's id for it."""
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        group_store.create("soc-team", roles=["data_analyst"], description="Old")
        group_store.update("soc-team", source_provider="test-sso", source_id="g-soc")
        groups = [GroupInfo(id="g-soc", name="SOC Team", email="", description="New")]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        assert (result["updated"], result["groups_skipped"]) == (1, 0)
        stored = group_store.get("soc-team")
        assert (stored.source_provider, stored.source_id) == ("test-sso", "g-soc")
        assert (stored.roles, stored.description) == (["data_analyst"], "New")

    async def test_a_group_linked_to_another_provider_keeps_its_provider(
        self, registries, api_provider, audit_events
    ):
        """Okta links on names, so one of its groups can carry the id another IdP's group has."""
        provider_registry, group_store = registries
        provider_registry.create("okta", api_provider)
        group_store.create("soc-corp.example", roles=["admin"], description="Ours")
        group_store.update("soc-corp.example", source_provider="keycloak", source_id="SOC Analysts")
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [GroupInfo(id="SOC Analysts", name="SOC Analysts", email="soc@corp.example")]

        result = await sync_provider(
            "okta",
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, groups),
            metrics=SyncMetrics(manager),
        )

        assert (result["updated"], result["groups_skipped"], result["status"]) == (
            0,
            1,
            "partial",
        )
        stored = group_store.get("soc-corp.example")
        assert (stored.source_provider, stored.source_id, stored.description) == (
            "keycloak",
            "SOC Analysts",
            "Ours",
        )
        assert _sync_skips(manager) == {"provider_mismatch": 1.0}
        assert provider_registry.get("okta").last_sync_status == (
            "partial: 1 of 1 groups skipped, their stored copy is linked to another provider"
        )
        (line,) = [
            e
            for e in audit_events
            if e["event"] == "OIDC group sync skipped a group linked to another provider"
        ]
        assert (line["provider"], line["linked_provider"], line["reason"], line["group"]) == (
            "okta",
            "keycloak",
            "provider_mismatch",
            "soc-corp.example",
        )
        assert "soc@corp.example" not in json.dumps(audit_events, default=str)

    async def test_a_differently_named_group_linked_to_another_provider_is_not_duplicated(
        self,
        api_provider: OIDCProvider,
        audit_events: list[dict],
        registries: tuple[OIDCProviderRegistry, GroupStore],
    ):
        provider_registry, group_store = registries
        provider_registry.create(name="okta", provider=api_provider)
        group_store.create(description="Ours", name="soc", roles=["admin"])
        group_store.update(name="soc", source_id="SOC Analysts", source_provider="keycloak")
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [GroupInfo(email="", id="SOC Analysts", name="SOC Analysts")]

        result = await sync_provider(
            adapter=FakeAdapter(groups=groups, provider=api_provider),
            group_store=group_store,
            metrics=SyncMetrics(manager),
            provider_name="okta",
            provider_registry=provider_registry,
        )

        stored = [
            (group.name, group.source_provider, group.source_id, group.description)
            for group in group_store.list()
        ]
        skipped = [
            (event["group"], event["linked_provider"], event["reason"])
            for event in audit_events
            if event["event"] == "OIDC group sync skipped a group linked to another provider"
        ]
        assert (result["created"], result["groups_skipped"], result["status"]) == (0, 1, "partial")
        assert (stored, _sync_skips(manager), skipped) == (
            [("soc", "keycloak", "SOC Analysts", "Ours")],
            {"provider_mismatch": 1.0},
            [("soc", "keycloak", "provider_mismatch")],
        )

    @pytest.mark.parametrize(
        ("stamp", "syncing", "bindings"),
        [
            pytest.param(SCIM_SOURCE_PROVIDER, "entra", {SCIM_SOURCE_PROVIDER: "entra"}, id="scim"),
            pytest.param("entra", "entra-legacy", {"entra-legacy": "entra"}, id="reverse"),
        ],
    )
    async def test_a_group_a_binding_joins_to_this_provider_syncs(
        self, registries, api_provider, stamp, syncing, bindings
    ):
        """auth.source_provider_bindings declares two names one identity source, in both directions."""
        provider_registry, group_store = registries
        provider_registry.create(syncing, api_provider)
        group_store.create(
            "soc-team", roles=["data_analyst"], source_provider=stamp, source_id="g-soc"
        )
        groups = [GroupInfo(id="g-soc", name="SOC Team", email="", description="From Entra")]

        result = await sync_provider(
            syncing,
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, groups),
            bindings=bindings,
        )

        assert (result["updated"], result["groups_skipped"], result["status"]) == (1, 0, "ok")
        stored = group_store.get("soc-team")
        assert (stored.source_provider, stored.source_id) == (stamp, "g-soc")
        assert (stored.roles, stored.description) == (["data_analyst"], "From Entra")

    async def test_a_scim_group_bound_to_another_provider_is_still_skipped(
        self, registries, api_provider
    ):
        provider_registry, group_store = registries
        provider_registry.create("entra", api_provider)
        group_store.create(
            "soc-team", roles=["admin"], source_provider=SCIM_SOURCE_PROVIDER, source_id="g-soc"
        )
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [GroupInfo(id="g-soc", name="SOC Team", email="")]

        result = await sync_provider(
            "entra",
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, groups),
            metrics=SyncMetrics(manager),
            bindings={SCIM_SOURCE_PROVIDER: "okta"},
        )

        assert (result["updated"], result["groups_skipped"], result["status"]) == (
            0,
            1,
            "partial",
        )
        assert group_store.get("soc-team").source_provider == SCIM_SOURCE_PROVIDER
        assert _sync_skips(manager) == {"provider_mismatch": 1.0}

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


class TestSyncNeverCreatesAGroupCrossingTheNamespaces:
    """An API key resolves a group identifier by name, then by id, so a new group's name may not be a stored group's id, nor its id a stored group's name."""

    @pytest.mark.parametrize(
        "case",
        SYNC_IDENTIFIER_COLLISION_CASES,
        ids=[case["id"] for case in SYNC_IDENTIFIER_COLLISION_CASES],
    )
    async def test_it_is_skipped_as_name_taken(
        self,
        api_provider: OIDCProvider,
        audit_events: list[dict],
        registries: tuple[OIDCProviderRegistry, GroupStore],
        case: SyncIdentifierCollisionCase,
    ):
        provider_registry, group_store = registries
        provider_registry.create(name="test-sso", provider=api_provider)
        group_store.create(members=[], name=case["stored_name"], roles=["admin"])
        group_store.update(name=case["stored_name"], **case["stored_link"])
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [GroupInfo(email="", id=case["group_id"], name=case["group_name"])]

        result = await sync_provider(
            adapter=FakeAdapter(groups=groups, provider=api_provider),
            group_store=group_store,
            metrics=SyncMetrics(manager),
            provider_name="test-sso",
            provider_registry=provider_registry,
        )

        skipped = [
            (event["group"], event["held_by"])
            for event in audit_events
            if event["event"]
            == "OIDC group sync skipped a group whose name or id is another group's id or name"
        ]
        stored = [group.name for group in group_store.list()]
        assert (result["created"], _sync_skips(manager), skipped, stored) == (
            0,
            {"name_taken": 1.0},
            [("soc-team", case["stored_name"])],
            [case["stored_name"]],
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

        synced = group_store.get("ops-example.com")
        provider = provider_registry.get("test-sso")
        assert synced is not None
        assert provider is not None
        assert result["error"] is None
        assert result["created"] == 1
        assert synced.source_id == "g2"
        assert stored.read_text(encoding="utf-8") == self._BAD_SCOPE
        assert "OIDC group sync skipped a group whose stored copy does not load" in warnings
        assert result["groups_skipped"] == 1
        assert provider.last_sync_status == (
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


class TestSyncOverAGroupTheStoreCannotWrite:
    """One group the store cannot write must not end the sync or leave the provider unstamped."""

    # U+0085 survives no YAML round trip, so the store refuses to write it.
    _UNWRITABLE = "a\x85b"

    async def test_a_description_yaml_cannot_carry_is_skipped_and_the_rest_sync(
        self, tmp_path, api_provider
    ):
        provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider_registry.create("test-sso", api_provider)
        group_store = GroupStore(tmp_path / "groups")
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [
            GroupInfo(id="g1", name="admins", email="admins@example.com"),
            GroupInfo(id="g2", name="bad", email="bad@example.com", description=self._UNWRITABLE),
            GroupInfo(id="g3", name="ops", email="ops@example.com"),
        ]

        result = await sync_provider(
            "test-sso",
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, groups),
            metrics=SyncMetrics(manager),
        )

        provider = provider_registry.get("test-sso")
        assert provider is not None
        assert [g.name for g in group_store.list()] == ["admins-example.com", "ops-example.com"]
        assert (result["created"], result["groups_skipped"], result["status"]) == (2, 1, "partial")
        assert result["error"] is None
        assert provider.last_sync_at
        assert provider.last_sync_status == (
            "partial: 1 of 3 groups skipped, the store could not write them"
        )
        assert _sync_skips(manager) == {"write_failed": 1.0}

    async def test_a_linked_group_whose_update_cannot_be_written_is_skipped(
        self, tmp_path, api_provider
    ):
        provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider_registry.create("test-sso", api_provider)
        group_store = GroupStore(tmp_path / "groups")
        group_store.create("admins", roles=["admin"], source_id="g1", source_provider="test-sso")
        group_store.create("ops", roles=["ops"], source_id="g2", source_provider="test-sso")
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        groups = [
            GroupInfo(id="g1", name="admins", description=self._UNWRITABLE),
            GroupInfo(id="g2", name="ops", description="Operators"),
        ]

        result = await sync_provider(
            "test-sso",
            provider_registry,
            group_store,
            adapter=FakeAdapter(api_provider, groups),
            metrics=SyncMetrics(manager),
        )

        ops = group_store.get("ops")
        assert ops is not None
        assert (result["updated"], result["groups_skipped"], ops.description) == (1, 1, "Operators")
        assert _sync_skips(manager) == {"write_failed": 1.0}

    async def test_a_volume_that_refuses_one_create_skips_that_group(self, tmp_path, api_provider):
        provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider_registry.create("test-sso", api_provider)
        group_store = CreateFailingGroupStore(
            tmp_path / "groups",
            error=OSError(28, "No space left on device"),
            fail_on="bad-example.com",
        )
        groups = [
            GroupInfo(id="g1", name="admins", email="admins@example.com"),
            GroupInfo(id="g2", name="bad", email="bad@example.com"),
            GroupInfo(id="g3", name="ops", email="ops@example.com"),
        ]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        assert [g.name for g in group_store.list()] == ["admins-example.com", "ops-example.com"]
        assert (result["created"], result["groups_skipped"], result["status"]) == (2, 1, "partial")

    async def test_any_other_store_failure_ends_the_run_recorded_as_an_error(
        self, audit_events, tmp_path, api_provider
    ):
        """The failure is neither a group's own nor a write, so the run stops, keeps its tally and stamps."""
        provider_registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider_registry.create("test-sso", api_provider)
        group_store = CreateFailingGroupStore(
            tmp_path / "groups",
            error=RuntimeError("store-sentinel-4417"),
            fail_on="bad-example.com",
        )
        groups = [
            GroupInfo(id="g1", name="admins", email="admins@example.com"),
            GroupInfo(id="g2", name="bad", email="bad@example.com"),
            GroupInfo(id="g3", name="ops", email="ops@example.com"),
        ]

        result = await sync_provider(
            "test-sso", provider_registry, group_store, adapter=FakeAdapter(api_provider, groups)
        )

        provider = provider_registry.get("test-sso")
        assert provider is not None
        # The group created before the failure is reported, so the caller still reconciles it.
        assert (result["status"], result["created"], result["error"]) == (
            "error",
            1,
            STORE_FAILURE_MESSAGE,
        )
        assert [g.name for g in group_store.list()] == ["admins-example.com"]
        assert provider.last_sync_at
        assert (provider.last_sync_status, provider.sync_error) == ("error", STORE_FAILURE_MESSAGE)
        (line,) = [e for e in audit_events if e["event"] == "OIDC group sync failed"]
        assert line["error_type"] == "RuntimeError"
        assert "store-sentinel-4417" not in json.dumps(audit_events, default=str)


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
        assert result["status"] == "partial"
        assert [g.name for g in group_store.list()] == ["ops-example.com"]
        assert group_store.get("ops-example.com").source_id == "g2"
        assert provider_registry.get("test-sso").last_sync_status == (
            "partial: 1 of 2 groups skipped, their identifier makes no valid group name"
        )
        assert _sync_skips(manager) == {"invalid_name": 1.0}


class TestSyncRefusesAGroupWithNoId:
    """Only the id links a stored group to a provider group, so a group with none syncs nowhere."""

    NAMES = ("Night Shift", "Day Shift")

    @classmethod
    def _fake(cls, provider, tmp_path, monkeypatch):
        return FakeAdapter(provider, [GroupInfo(id="", name=name, email="") for name in cls.NAMES])

    @classmethod
    def _okta(cls, provider, tmp_path, monkeypatch):
        # Okta links on the group name, so a group with only an id and a description has none.
        items = [
            {"id": f"00g{index}", "profile": {"description": name}}
            for index, name in enumerate(cls.NAMES)
        ]
        return FakeAdapter(provider, [_group_info_from_okta(item) for item in items])

    @classmethod
    def _mock_directory(cls, provider, tmp_path, monkeypatch):
        fixture = tmp_path / "directory.json"
        groups = [{"name": name, "email": ""} for name in cls.NAMES]
        fixture.write_text(json.dumps({"groups": groups}), encoding="utf-8")
        monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(fixture))
        return MockDirectoryAdapter(provider)

    @pytest.mark.parametrize("directory", ["_fake", "_okta", "_mock_directory"])
    async def test_each_is_skipped_counted_and_logged_once_by_count(
        self, registries, api_provider, tmp_path, monkeypatch, audit_events, directory
    ):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)
        manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
        adapter = getattr(self, directory)(api_provider, tmp_path, monkeypatch)

        result = await sync_provider(
            "test-sso",
            provider_registry,
            group_store,
            adapter=adapter,
            metrics=SyncMetrics(manager),
        )

        assert (result["created"], result["updated"], result["groups_skipped"]) == (0, 0, 2)
        assert result["status"] == "partial"
        assert group_store.list() == []
        assert _sync_skips(manager) == {"no_id": 2.0}
        assert provider_registry.get("test-sso").last_sync_status == (
            "partial: 2 of 2 groups skipped, they carry no id to link them by"
        )
        (line,) = [
            e for e in audit_events if e["event"] == "OIDC group sync skipped groups with no id"
        ]
        assert (line["count"], line["provider"]) == (2, "test-sso")
        assert not [name for name in self.NAMES if name in json.dumps(audit_events, default=str)]


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
        assert result["status"] == "skipped"

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

        # A RuntimeError carries no socket error, so nothing says the API was unreachable.
        assert result["error"] == (
            "The directory API gave no usable answer; the engine log has the reason"
        )
        assert result["skipped"] is None
        assert result["created"] == 0
        assert result["total"] == 0
        assert result["status"] == "error"

    async def test_adapter_error_sets_provider_sync_error(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        await sync_provider("test-sso", provider_registry, group_store, adapter=error_adapter)

        updated_provider = provider_registry.get("test-sso")
        assert updated_provider is not None
        assert updated_provider.last_sync_status == "error"
        assert updated_provider.sync_error == (
            "The directory API gave no usable answer; the engine log has the reason"
        )
        assert updated_provider.last_sync_at != ""

    async def test_adapter_error_does_not_create_groups(self, registries, api_provider):
        provider_registry, group_store = registries
        provider_registry.create("test-sso", api_provider)

        error_adapter = ErrorAdapter(api_provider)
        await sync_provider("test-sso", provider_registry, group_store, adapter=error_adapter)

        assert group_store.list() == []


class TestSyncWithNoDirectoryCredential:
    """An api-mode provider with nothing to list with is not 'ok, 0 groups'."""

    @pytest.mark.parametrize("provider_type", ["entra_id", "okta"])
    async def test_reports_not_configured_in_the_result_and_on_the_provider(
        self, registries, provider_type
    ):
        provider_registry, group_store = registries
        provider = OIDCProvider(
            type=provider_type,
            enabled=True,
            issuer="https://sso.example.com",
            groups=GroupResolutionConfig(mode="api"),
        )
        provider_registry.create("no-credential", provider)

        result = await sync_provider("no-credential", provider_registry, group_store)

        assert result["skipped"] == NOT_CONFIGURED_MESSAGE
        assert result["error"] is None
        assert result["status"] == "not_configured"
        assert (result["created"], result["updated"], result["total"]) == (0, 0, 0)
        stored = provider_registry.get("no-credential")
        assert stored is not None
        assert stored.last_sync_status == "not_configured"
        assert stored.sync_error == NOT_CONFIGURED_MESSAGE
        assert stored.last_sync_at != ""
        assert group_store.list() == []

    async def test_a_google_provider_with_no_service_account_is_no_fault(self, registries):
        """Each login reads its groups with the user's token, the documented default."""
        provider_registry, group_store = registries
        provider = OIDCProvider(
            type="google",
            enabled=True,
            issuer="https://accounts.google.com",
            groups=GroupResolutionConfig(enrich_on_login=True, mode="api"),
        )
        provider_registry.create("google-default", provider)

        result = await sync_provider("google-default", provider_registry, group_store)

        stored = provider_registry.get("google-default")
        assert stored is not None
        assert (stored.last_sync_status, stored.sync_error) == ("user_token", "")
        assert stored.last_sync_at != ""
        assert (result["status"], result["error"]) == ("user_token", None)
        assert result["skipped"] == NO_DIRECTORY_MESSAGE
        assert "configure" not in result["skipped"]
        assert group_store.list() == []

    async def test_a_later_sync_with_a_credential_clears_the_state(self, registries, api_provider):
        provider_registry, group_store = registries
        unconfigured = OIDCProvider(
            type="okta",
            enabled=True,
            issuer="https://sso.example.com",
            groups=GroupResolutionConfig(mode="api"),
        )
        provider_registry.create("test-sso", unconfigured)
        await sync_provider("test-sso", provider_registry, group_store)

        adapter = FakeAdapter(api_provider, [GroupInfo(id="g1", name="admins")])
        await sync_provider("test-sso", provider_registry, group_store, adapter=adapter)

        stored = provider_registry.get("test-sso")
        assert stored is not None
        assert (stored.last_sync_status, stored.sync_error) == ("ok", "")


_STORED_CREDENTIAL_PROVIDERS = pytest.mark.parametrize(
    ("provider_type", "groups"),
    [
        (
            "okta",
            {"api_token_path": "oidc/acme/groups_api_token", "okta_domain": "example.okta.com"},
        ),
        (
            "entra_id",
            {
                "client_secret_path": "oidc/acme/groups_client_secret",
                "tenant_id": "test-tenant",
            },
        ),
        ("google", {"service_account_json_path": "oidc/acme/groups_service_account_json"}),
    ],
    ids=["okta", "entra_id", "google"],
)


def _stored_credential_provider(*, provider_type: str, groups: dict[str, str]) -> OIDCProvider:
    """An api-mode provider whose directory credential is held only in the secret store."""
    return OIDCProvider.model_validate(
        {
            "client_id": "app-id",
            "groups": {"mode": "api", **groups},
            "issuer": "https://sso.example.com",
            "type": provider_type,
        }
    )


class TestSyncWhenTheSecretStoreDidNotAnswer:
    """A credential the store holds but did not hand over is a failed run, never 'not configured'."""

    @_STORED_CREDENTIAL_PROVIDERS
    async def test_the_run_is_an_error_that_names_the_secret_store(
        self, registries, provider_type, groups
    ):
        provider_registry, group_store = registries
        provider = _stored_credential_provider(provider_type=provider_type, groups=groups)
        provider_registry.create("stored", provider)

        result = await sync_provider(
            "stored", provider_registry, group_store, secrets=UnreachableSecretStore()
        )

        stored = provider_registry.get("stored")
        assert stored is not None
        assert stored.last_sync_status == "error"
        assert "secret store did not answer" in stored.sync_error
        assert result["status"] == "error"
        assert stored.sync_error == result["error"]
        assert result["skipped"] is None

    @_STORED_CREDENTIAL_PROVIDERS
    async def test_one_sync_asks_the_store_once(
        self, audit_events, registries, provider_type, groups
    ):
        provider_registry, group_store = registries
        provider = _stored_credential_provider(provider_type=provider_type, groups=groups)
        provider_registry.create("stored", provider)

        await sync_provider(
            "stored", provider_registry, group_store, secrets=UnreachableSecretStore()
        )

        unreadable = [
            e
            for e in audit_events
            if e["event"] == "OIDC credential unreadable from the secret store"
        ]
        assert len(unreadable) == 1


class TestSyncWhenTheSecretStoreRefused:
    """A store that answered and refused is a failed run that points at its policy, not its reachability."""

    @_STORED_CREDENTIAL_PROVIDERS
    async def test_the_run_is_an_error_that_names_the_refusal(
        self, registries, provider_type, groups
    ):
        provider_registry, group_store = registries
        provider = _stored_credential_provider(provider_type=provider_type, groups=groups)
        provider_registry.create("stored", provider)

        result = await sync_provider(
            "stored", provider_registry, group_store, secrets=PolicyRefusingSecretStore()
        )

        stored = provider_registry.get("stored")
        assert stored is not None
        assert (result["status"], stored.last_sync_status) == ("error", "error")
        assert result["error"] == (
            "The secret store refused access to the directory credential, so check the "
            "engine's secrets policy; the engine log has the reason"
        )
        assert stored.sync_error == result["error"]
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
