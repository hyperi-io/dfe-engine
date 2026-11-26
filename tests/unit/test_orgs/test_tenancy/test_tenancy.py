#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_tenancy/test_tenancy.py
#  Purpose:      Tests for the tenant a record naming no org takes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the tenant a record naming no org takes."""

from pathlib import Path

import pytest

from dfe_engine.orgs.tenancy import Tenancy, default_tenant_id
from tests.unit.test_orgs.factories import (
    make_home_org_store,
    make_org,
    make_org_registry,
    make_tenancy,
)

ACME = make_org(name="acme", org_ids=["acme-prod", "acme-dev"])


class TestTenancy:
    def test_tenant_id(self):
        tenancy = make_tenancy(orgs=(make_org(name="acme", org_ids=["acme-prod"]),))
        assert (tenancy.tenant_id(None), tenancy.tenant_id("acme")) == ("unassigned", "acme-prod")

    @pytest.mark.parametrize(
        ("orgs", "home", "expected"),
        [
            ((ACME,), "acme", "acme-prod"),
            ((make_org(name="acme"),), "acme", "acme"),
            ((ACME,), None, "unassigned"),
            ((ACME,), "ghost", "unassigned"),
            ((make_org(name="acme", org_ids=["acme-prod"], enabled=False),), "acme", "unassigned"),
            ((), "acme", "unassigned"),
        ],
        ids=[
            "your-org-first-tenant-id",
            "your-org-with-no-org-ids-is-its-name",
            "none-named",
            "named-org-not-registered",
            "named-org-disabled",
            "your-org-gone-from-the-registry",
        ],
    )
    def test_a_record_naming_no_org_takes_your_org_s_first_tenant_id(self, orgs, home, expected):
        tenancy = make_tenancy(orgs=orgs, home=home)

        assert (tenancy.default_tenant_id(), tenancy.tenant_id(None)) == (expected, expected)

    def test_a_caller_holding_no_tenancy_compiles_unassigned(self):
        assert default_tenant_id(None) == "unassigned"


class TestTenancyRead:
    def test_it_reads_your_org(self, tmp_path: Path):
        home = make_home_org_store(config_dir=tmp_path / "config")
        home.set(actor="admin", name="acme")
        registry = make_org_registry(home=home, path=tmp_path / "orgs")
        registry.create("acme", org_ids=["acme-prod"])

        assert Tenancy.read(registry).default_tenant_id() == "acme-prod"

    def test_an_unreadable_doc_reads_as_none_named(self, tmp_path: Path):
        doc = tmp_path / "config" / "governance" / "settings" / "organisation.yaml"
        doc.parent.mkdir(parents=True)
        doc.write_text("name: [acme]\n", encoding="utf-8")
        registry = make_org_registry(
            home=make_home_org_store(config_dir=tmp_path / "config"), path=tmp_path / "orgs"
        )
        registry.create("acme", org_ids=["acme-prod"])

        tenancy = Tenancy.read(registry)

        assert (tenancy.home, tenancy.default_tenant_id()) == (None, "unassigned")
