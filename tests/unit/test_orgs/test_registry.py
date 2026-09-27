#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_registry.py
#  Purpose:      Tests for YAML-backed OrgRegistry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import time

import pytest

from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    """OrgRegistry backed by a temporary directory."""
    return OrgRegistry(tmp_path / "orgs")


# ---------------------------------------------------------------------------
# OrgRegistry.create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_returns_org(self, registry):
        org = registry.create("acme")
        assert isinstance(org, Org)
        assert org.name == "acme"

    def test_create_with_org_ids(self, registry):
        org = registry.create("acme", org_ids=["acme", "acme-sub"])
        assert org.org_ids == ["acme", "acme-sub"]

    def test_create_with_display_name(self, registry):
        org = registry.create("acme", display_name="Acme Corp")
        assert org.display_name == "Acme Corp"

    def test_create_default_enabled(self, registry):
        org = registry.create("acme")
        assert org.enabled is True

    def test_create_empty_org_ids_by_default(self, registry):
        org = registry.create("acme")
        assert org.org_ids == []

    def test_create_duplicate_raises(self, registry):
        registry.create("acme")
        with pytest.raises(ValueError, match="acme"):
            registry.create("acme")

    @pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "has space"])
    def test_create_rejects_name_that_is_not_a_filename_stem(self, name, registry, tmp_path):
        with pytest.raises(ValueError, match="Invalid org name"):
            registry.create(name=name)
        assert list(tmp_path.rglob("*.yaml")) == []

    @pytest.mark.parametrize("name", ["acme", "Acme-Corp_2", "acme.prod", "7eleven"])
    def test_create_accepts_valid_name(self, name, registry):
        assert registry.create(name=name).name == name

    def test_create_sets_timestamps(self, registry):
        org = registry.create("acme")
        assert org.created_at != ""
        assert org.updated_at != ""

    def test_create_writes_yaml_file(self, registry, tmp_path):
        registry.create("acme")
        yaml_file = tmp_path / "orgs" / "acme.yaml"
        assert yaml_file.exists()

    def test_create_name_not_in_yaml(self, registry, tmp_path):
        """Name is the filename stem — NOT stored inside the YAML."""
        registry.create("acme")
        yaml_file = tmp_path / "orgs" / "acme.yaml"
        content = yaml_file.read_text()
        # "name:" as a top-level YAML key should not appear; display_name is fine
        lines = content.strip().splitlines()
        top_keys = [line.split(":")[0] for line in lines if not line.startswith(" ")]
        assert "name" not in top_keys

    def test_create_creates_dir_if_not_exists(self, tmp_path):
        nested = tmp_path / "deep" / "nested" / "orgs"
        reg = OrgRegistry(nested)
        reg.create("acme")
        assert (nested / "acme.yaml").exists()


# ---------------------------------------------------------------------------
# OrgRegistry.get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_existing_returns_org(self, registry):
        registry.create("acme")
        org = registry.get("acme")
        assert org is not None
        assert org.name == "acme"

    def test_get_nonexistent_returns_none(self, registry):
        result = registry.get("nobody")
        assert result is None

    def test_get_name_from_filename_not_yaml(self, registry):
        """Name must come from the filename stem, not YAML content."""
        registry.create("acme")
        org = registry.get("acme")
        assert org.name == "acme"

    def test_get_preserves_org_ids(self, registry):
        registry.create("acme", org_ids=["acme", "acme-sub"])
        org = registry.get("acme")
        assert org.org_ids == ["acme", "acme-sub"]

    def test_get_preserves_enabled_state(self, registry):
        registry.create("acme")
        registry.update("acme", enabled=False)
        org = registry.get("acme")
        assert org.enabled is False

    def test_get_preserves_display_name(self, registry):
        registry.create("acme", display_name="Acme Corp")
        org = registry.get("acme")
        assert org.display_name == "Acme Corp"


# ---------------------------------------------------------------------------
# OrgRegistry.list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_empty_returns_empty(self, registry):
        assert registry.list() == []

    def test_list_single_org(self, registry):
        registry.create("acme")
        orgs = registry.list()
        assert len(orgs) == 1
        assert orgs[0].name == "acme"

    def test_list_multiple_orgs(self, registry):
        registry.create("charlie-co")
        registry.create("acme")
        registry.create("beta-inc")
        orgs = registry.list()
        assert len(orgs) == 3
        names = {o.name for o in orgs}
        assert names == {"acme", "beta-inc", "charlie-co"}

    def test_list_returns_org_objects(self, registry):
        registry.create("acme")
        for org in registry.list():
            assert isinstance(org, Org)


# ---------------------------------------------------------------------------
# OrgRegistry.update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_display_name(self, registry):
        registry.create("acme")
        org = registry.update("acme", display_name="Acme Corporation")
        assert org.display_name == "Acme Corporation"

    def test_update_org_ids(self, registry):
        registry.create("acme", org_ids=["acme"])
        org = registry.update("acme", org_ids=["acme", "acme-sub", "acme-new"])
        assert org.org_ids == ["acme", "acme-sub", "acme-new"]

    def test_update_disable_org(self, registry):
        registry.create("acme")
        org = registry.update("acme", enabled=False)
        assert org.enabled is False

    def test_update_enable_org(self, registry):
        registry.create("acme")
        registry.update("acme", enabled=False)
        org = registry.update("acme", enabled=True)
        assert org.enabled is True

    def test_update_returns_org(self, registry):
        registry.create("acme")
        result = registry.update("acme", enabled=False)
        assert isinstance(result, Org)
        assert result.enabled is False

    def test_update_persists_to_disk(self, registry):
        registry.create("acme")
        registry.update("acme", enabled=False, display_name="Updated")
        org = registry.get("acme")
        assert org.enabled is False
        assert org.display_name == "Updated"

    def test_update_nonexistent_raises(self, registry):
        with pytest.raises(KeyError, match="nobody"):
            registry.update("nobody", enabled=False)

    def test_update_updates_timestamp(self, registry):
        registry.create("acme")
        time.sleep(0.01)
        updated = registry.update("acme", enabled=True)
        assert updated.updated_at != ""


# ---------------------------------------------------------------------------
# OrgRegistry.delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_org(self, registry):
        registry.create("acme")
        registry.delete("acme")
        assert registry.get("acme") is None

    def test_delete_removes_file(self, registry, tmp_path):
        registry.create("acme")
        yaml_file = tmp_path / "orgs" / "acme.yaml"
        assert yaml_file.exists()
        registry.delete("acme")
        assert not yaml_file.exists()

    def test_delete_nonexistent_raises(self, registry):
        with pytest.raises(KeyError, match="nobody"):
            registry.delete("nobody")

    def test_delete_does_not_affect_other_orgs(self, registry):
        registry.create("acme")
        registry.create("beta")
        registry.delete("acme")
        assert registry.get("beta") is not None
        assert len(registry.list()) == 1


# ---------------------------------------------------------------------------
# on_change
# ---------------------------------------------------------------------------


class TestOnChange:
    def test_every_write_tells_the_listener(self, registry):
        """Create, update and delete each land a call, after the write is on disk."""
        seen: list[list[str]] = []
        registry.on_change(lambda: seen.append([o.name for o in registry.list()]))

        registry.create("acme")
        registry.update("acme", display_name="Acme")
        registry.delete("acme")

        assert seen == [["acme"], ["acme"], []]

    def test_a_refused_write_tells_nobody(self, registry):
        calls: list[None] = []
        registry.on_change(lambda: calls.append(None))
        registry.create("acme")

        with pytest.raises(ValueError):
            registry.create("acme")
        with pytest.raises(KeyError):
            registry.update("nobody", display_name="x")
        with pytest.raises(KeyError):
            registry.delete("nobody")

        assert len(calls) == 1

    def test_a_listener_that_raises_does_not_fail_the_write(self, registry):
        def broken() -> None:
            raise RuntimeError("listener down")

        calls: list[None] = []
        registry.on_change(broken)
        registry.on_change(lambda: calls.append(None))

        org = registry.create("acme")

        assert org.name == "acme"
        assert registry.get("acme") is not None
        assert len(calls) == 1
