#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_registry.py
#  Purpose:      Tests for OIDCProviderRegistry YAML-backed CRUD
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import pytest

from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.yaml_utils import yaml_load


def _make_google_provider() -> OIDCProvider:
    return OIDCProvider(
        type="google",
        enabled=True,
        display_name="Google Workspace",
        issuer="https://accounts.google.com",
        client_id_env="GOOGLE_CLIENT_ID",
        groups=GroupResolutionConfig(
            mode="api",
            service_account_json_env="GOOGLE_SA_JSON",
            admin_email="admin@example.com",
            domain="example.com",
        ),
    )


def _make_generic_provider(*, enabled: bool = True) -> OIDCProvider:
    return OIDCProvider(
        type="generic",
        enabled=enabled,
        display_name="Generic SSO",
        issuer="https://sso.example.com",
        client_id_env="SSO_CLIENT_ID",
    )


class TestOIDCProviderRegistryCreate:
    def test_create_success(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider = _make_generic_provider()
        result = registry.create("my-sso", provider)
        assert result.type == "generic"
        assert result.display_name == "Generic SSO"

    def test_create_writes_yaml_file(self, tmp_path):
        oidc_dir = tmp_path / "oidc"
        registry = OIDCProviderRegistry(oidc_dir)
        registry.create("my-sso", _make_generic_provider())
        assert (oidc_dir / "my-sso.yaml").exists()

    def test_create_duplicate_raises_value_error(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        with pytest.raises(ValueError, match="my-sso"):
            registry.create("my-sso", _make_generic_provider())

    def test_create_creates_dir_if_not_exists(self, tmp_path):
        oidc_dir = tmp_path / "new" / "oidc"
        assert not oidc_dir.exists()
        registry = OIDCProviderRegistry(oidc_dir)
        registry.create("test", _make_generic_provider())
        assert oidc_dir.exists()

    def test_create_name_not_stored_as_field_in_yaml(self, tmp_path):
        """Provider name is the filename stem — NOT a field in the YAML body."""
        oidc_dir = tmp_path / "oidc"
        registry = OIDCProviderRegistry(oidc_dir)
        registry.create("my-sso", _make_generic_provider())
        data = yaml_load(oidc_dir / "my-sso.yaml")
        # The name field doesn't exist on OIDCProvider — confirmed absent
        assert "name" not in data

    def test_create_returns_provider_object(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        provider = _make_google_provider()
        result = registry.create("google", provider)
        assert isinstance(result, OIDCProvider)
        assert result.type == "google"


class TestOIDCProviderRegistryGet:
    def test_get_existing(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        result = registry.get("my-sso")
        assert result is not None
        assert result.type == "generic"
        assert result.issuer == "https://sso.example.com"

    def test_get_nonexistent_returns_none(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        assert registry.get("nonexistent") is None

    def test_get_google_provider_preserves_group_config(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("google", _make_google_provider())
        result = registry.get("google")
        assert result is not None
        assert result.groups.mode == "api"
        assert result.groups.domain == "example.com"

    def test_get_persists_across_fresh_registry_instance(self, tmp_path):
        """Data persists to YAML and can be read by a new registry instance."""
        oidc_dir = tmp_path / "oidc"
        registry1 = OIDCProviderRegistry(oidc_dir)
        registry1.create("my-sso", _make_generic_provider())

        registry2 = OIDCProviderRegistry(oidc_dir)
        result = registry2.get("my-sso")
        assert result is not None
        assert result.display_name == "Generic SSO"


class TestOIDCProviderRegistryList:
    def test_list_empty(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        assert registry.list() == []

    def test_list_returns_name_provider_tuples(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("alpha", _make_generic_provider())
        results = registry.list()
        assert len(results) == 1
        name, provider = results[0]
        assert name == "alpha"
        assert isinstance(provider, OIDCProvider)

    def test_list_sorted_by_name(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("zebra", _make_generic_provider())
        registry.create("alpha", _make_generic_provider())
        registry.create("middle", _make_generic_provider())
        names = [name for name, _ in registry.list()]
        assert names == ["alpha", "middle", "zebra"]

    def test_list_multiple_providers(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("sso", _make_generic_provider())
        registry.create("google", _make_google_provider())
        results = registry.list()
        assert len(results) == 2


class TestOIDCProviderRegistryUpdate:
    def test_update_display_name(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        updated = registry.update("my-sso", display_name="Updated SSO")
        assert updated.display_name == "Updated SSO"

    def test_update_enabled_flag(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider(enabled=True))
        updated = registry.update("my-sso", enabled=False)
        assert updated.enabled is False

    def test_update_persists(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        registry.update("my-sso", display_name="New Name")
        result = registry.get("my-sso")
        assert result is not None
        assert result.display_name == "New Name"

    def test_update_preserves_other_fields(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        updated = registry.update("my-sso", display_name="New Name")
        assert updated.issuer == "https://sso.example.com"
        assert updated.type == "generic"

    def test_update_nonexistent_raises_key_error(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        with pytest.raises(KeyError, match="nonexistent"):
            registry.update("nonexistent", display_name="X")

    def test_update_sync_status_fields(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        updated = registry.update(
            "my-sso",
            last_sync_at="2026-01-01T00:00:00Z",
            last_sync_status="ok",
        )
        assert updated.last_sync_at == "2026-01-01T00:00:00Z"
        assert updated.last_sync_status == "ok"


class TestOIDCProviderRegistryDelete:
    def test_delete_existing(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("my-sso", _make_generic_provider())
        registry.delete("my-sso")
        assert registry.get("my-sso") is None

    def test_delete_removes_file(self, tmp_path):
        oidc_dir = tmp_path / "oidc"
        registry = OIDCProviderRegistry(oidc_dir)
        registry.create("my-sso", _make_generic_provider())
        registry.delete("my-sso")
        assert not (oidc_dir / "my-sso.yaml").exists()

    def test_delete_nonexistent_raises_key_error(self, tmp_path):
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        with pytest.raises(KeyError, match="nonexistent"):
            registry.delete("nonexistent")


class TestANameThatIsAPath:
    """Every lookup joins the name onto the providers directory, so one must not climb out."""

    NAME = "../elsewhere/outside"

    @pytest.fixture
    def outside(self, tmp_path):
        OIDCProviderRegistry(tmp_path / "elsewhere").create("outside", _make_generic_provider())
        return tmp_path / "elsewhere" / "outside.yaml"

    @pytest.fixture
    def registry(self, tmp_path):
        return OIDCProviderRegistry(tmp_path / "oidc")

    def test_get_finds_nothing(self, registry, outside):
        assert registry.get(self.NAME) is None

    def test_update_leaves_the_file_alone(self, registry, outside):
        before = outside.read_text()

        with pytest.raises(KeyError):
            registry.update(self.NAME, display_name="rewritten")

        assert outside.read_text() == before

    def test_delete_leaves_the_file_alone(self, registry, outside):
        with pytest.raises(KeyError):
            registry.delete(self.NAME)

        assert outside.exists()

    def test_create_writes_nothing(self, registry, tmp_path):
        with pytest.raises(ValueError, match="Invalid OIDC provider name"):
            registry.create("../elsewhere/planted", _make_generic_provider())

        assert not (tmp_path / "elsewhere" / "planted.yaml").exists()
