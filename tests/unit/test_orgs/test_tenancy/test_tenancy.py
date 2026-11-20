#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_tenancy/test_tenancy.py
#  Purpose:      Tests for the tenant a record naming no org takes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the tenant a record naming no org takes."""

from tests.unit.test_orgs.factories import make_org, make_tenancy


class TestTenancy:
    def test_tenant_id(self):
        tenancy = make_tenancy(orgs=(make_org(name="acme", org_ids=["acme-prod"]),))
        assert (tenancy.tenant_id(None), tenancy.tenant_id("acme")) == ("unassigned", "acme-prod")
