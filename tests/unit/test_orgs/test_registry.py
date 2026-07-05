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


# ---------------------------------------------------------------------------
# OrgRegistry domains (Task A)
# ---------------------------------------------------------------------------


class TestDomains:
    def test_create_default_empty_domains(self, registry):
        org = registry.create("acme")
        assert org.domains == []

    def test_create_normalises_domains_to_lowercase(self, registry):
        org = registry.create("acme", domains=["a.com", "B.com"])
        assert org.domains == ["a.com", "b.com"]

    def test_create_domains_separate_from_org_ids(self, registry):
        org = registry.create("acme", org_ids=["acme-tenant"], domains=["acme.com"])
        assert org.org_ids == ["acme-tenant"]
        assert org.domains == ["acme.com"]

    def test_create_domains_strip_and_dedupe(self, registry):
        org = registry.create("acme", domains=[" Acme.com ", "acme.com", ""])
        assert org.domains == ["acme.com"]

    def test_domains_persist_to_disk(self, registry):
        registry.create("acme", domains=["Acme.com"])
        org = registry.get("acme")
        assert org.domains == ["acme.com"]

    def test_update_adds_domains(self, registry):
        registry.create("acme")
        org = registry.update("acme", domains=["acme.com", "acme.io"])
        assert org.domains == ["acme.com", "acme.io"]

    def test_update_normalises_domains(self, registry):
        registry.create("acme", domains=["acme.com"])
        org = registry.update("acme", domains=["ACME.com", "New.IO"])
        assert org.domains == ["acme.com", "new.io"]

    def test_update_removes_domains(self, registry):
        registry.create("acme", domains=["acme.com", "acme.io"])
        org = registry.update("acme", domains=["acme.com"])
        assert org.domains == ["acme.com"]

    def test_update_clear_domains(self, registry):
        registry.create("acme", domains=["acme.com"])
        org = registry.update("acme", domains=[])
        assert org.domains == []

    def test_update_leaves_domains_untouched_when_not_supplied(self, registry):
        registry.create("acme", domains=["acme.com"])
        org = registry.update("acme", display_name="Acme Corp")
        assert org.domains == ["acme.com"]


# ---------------------------------------------------------------------------
# OrgRegistry.find_by_domain (Task B)
# ---------------------------------------------------------------------------


class TestFindByDomain:
    def test_resolves_claiming_org(self, registry):
        registry.create("acme", org_ids=["acme-tenant"], domains=["acme.com"])
        org = registry.find_by_domain("acme.com")
        assert org is not None
        assert org.name == "acme"
        assert org.org_ids == ["acme-tenant"]

    def test_case_insensitive(self, registry):
        registry.create("acme", domains=["acme.com"])
        org = registry.find_by_domain("ACME.com")
        assert org is not None
        assert org.name == "acme"

    def test_resolves_across_multiple_domains_on_one_org(self, registry):
        registry.create("acme", domains=["acme.com", "acme.io", "acme.co.uk"])
        assert registry.find_by_domain("acme.io").name == "acme"
        assert registry.find_by_domain("acme.co.uk").name == "acme"

    def test_resolves_correct_org_among_many(self, registry):
        registry.create("acme", domains=["acme.com"])
        registry.create("globex", domains=["globex.com", "globex.net"])
        assert registry.find_by_domain("globex.net").name == "globex"
        assert registry.find_by_domain("acme.com").name == "acme"

    def test_unclaimed_domain_returns_none(self, registry):
        registry.create("acme", domains=["acme.com"])
        assert registry.find_by_domain("nobody.com") is None

    def test_empty_domain_returns_none(self, registry):
        registry.create("acme", domains=["acme.com"])
        assert registry.find_by_domain("") is None
        assert registry.find_by_domain("   ") is None

    def test_no_orgs_returns_none(self, registry):
        assert registry.find_by_domain("acme.com") is None

    def test_tie_break_is_deterministic_first_by_name(self, registry):
        # Misconfiguration: two orgs claim the same domain. Resolution must be
        # stable - the org whose name sorts first wins, regardless of insert order.
        registry.create("zeta", domains=["shared.com"])
        registry.create("alpha", domains=["shared.com"])
        chosen = registry.find_by_domain("shared.com")
        assert chosen is not None
        assert chosen.name == "alpha"


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
