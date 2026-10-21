#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_default_org.py
#  Purpose:      The reserved default org every unbound record lands in
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every app stamps ``default`` on a record that names no org, so that org always exists."""

import pytest

from dfe_engine.orgs.models import DEFAULT_ORG_ID, DEFAULT_ORG_NAME, ReservedOrgError
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.seed import ensure_default_org
from dfe_engine.orgs.tenant_scope import org_tenant_ids


@pytest.fixture
def registry(tmp_path) -> OrgRegistry:
    return OrgRegistry(tmp_path / "orgs")


class TestEnsure:
    def test_a_fresh_registry_gets_the_default_org(self, registry):
        assert ensure_default_org(registry) is True

        org = registry.get(DEFAULT_ORG_NAME)
        assert org is not None
        assert org_tenant_ids(org) == [DEFAULT_ORG_ID]
        assert org.enabled is True

    def test_a_second_boot_writes_nothing(self, registry):
        ensure_default_org(registry)

        assert ensure_default_org(registry) is False

    def test_a_default_org_disabled_out_of_band_is_enabled_again(self, registry):
        # Written straight to the file, as a restored config volume could carry it.
        registry.create(DEFAULT_ORG_NAME, org_ids=["default"])
        path = registry._path(DEFAULT_ORG_NAME)
        path.write_text(path.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"))

        assert ensure_default_org(registry) is True
        assert registry.get(DEFAULT_ORG_NAME).enabled is True

    def test_its_display_name_is_the_operator_s_to_keep(self, registry):
        ensure_default_org(registry)
        registry.update(DEFAULT_ORG_NAME, display_name="Unassigned")

        assert ensure_default_org(registry) is False
        assert registry.get(DEFAULT_ORG_NAME).display_name == "Unassigned"

    def test_another_org_holding_the_tenant_id_stops_it(self, registry):
        # That org would read every record that names no org.
        registry.create("acme", org_ids=[DEFAULT_ORG_ID])

        with pytest.raises(ValueError, match="acme"):
            ensure_default_org(registry)


class TestReserved:
    def test_it_cannot_be_deleted(self, registry):
        ensure_default_org(registry)

        with pytest.raises(ReservedOrgError, match="cannot be deleted"):
            registry.delete(DEFAULT_ORG_NAME)
        assert registry.get(DEFAULT_ORG_NAME) is not None

    def test_it_cannot_be_disabled(self, registry):
        ensure_default_org(registry)

        with pytest.raises(ReservedOrgError, match="disabled"):
            registry.update(DEFAULT_ORG_NAME, enabled=False)
        assert registry.get(DEFAULT_ORG_NAME).enabled is True

    @pytest.mark.parametrize("org_ids", [["other"], []])
    def test_it_keeps_its_tenant_id(self, registry, org_ids):
        # An empty list reads as the org's name, which is still the tenant id.
        ensure_default_org(registry)

        if org_ids:
            with pytest.raises(ReservedOrgError, match="keeps the tenant id"):
                registry.update(DEFAULT_ORG_NAME, org_ids=org_ids)
            assert registry.get(DEFAULT_ORG_NAME).org_ids == [DEFAULT_ORG_ID]
        else:
            registry.update(DEFAULT_ORG_NAME, org_ids=org_ids)
            assert org_tenant_ids(registry.get(DEFAULT_ORG_NAME)) == [DEFAULT_ORG_ID]

    def test_it_may_carry_more_tenant_ids(self, registry):
        ensure_default_org(registry)

        registry.update(DEFAULT_ORG_NAME, org_ids=[DEFAULT_ORG_ID, "unknown"])

        assert org_tenant_ids(registry.get(DEFAULT_ORG_NAME)) == [DEFAULT_ORG_ID, "unknown"]

    def test_no_other_org_may_take_its_tenant_id(self, registry):
        ensure_default_org(registry)

        with pytest.raises(ValueError, match="collides"):
            registry.create("acme", org_ids=[DEFAULT_ORG_ID])

    def test_another_org_is_deleted_as_before(self, registry):
        ensure_default_org(registry)
        registry.create("acme")

        registry.delete("acme")

        assert registry.get("acme") is None
