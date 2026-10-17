#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_tenant_id_shape.py
#  Purpose:      Which tenant ids an org may declare
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An org's tenant ids are the ``_org_id`` values a shipper sends, taken verbatim.

So every shape a source can carry is accepted, and only what breaks the tenant fence
is refused: an empty id matches every untenanted row, a comma splits the ClickHouse
pin, and a control character or edge whitespace is a typo no source sends.
"""

import pytest
from pydantic import ValidationError

from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.settings import SeedOrg

VALID = [
    "acme",
    "t-acme-1",
    "test_org",
    "default",
    "3f2a9c1e-7b4d-4e8a-9f0e-2c6d1b8a5e3f",
    "acme.com",
    "Acme Corp",
    "o'brien",
    "aws:123456789012",
    "tenant/7",
    "unié",
]

INVALID = ["", " ", " acme", "acme ", "a,b", "acme\n", "ac\x00me", "\tacme"]


@pytest.fixture
def registry(tmp_path):
    return OrgRegistry(tmp_path / "orgs")


@pytest.mark.parametrize("tenant", VALID)
def test_every_shape_a_source_sends_is_accepted(tenant, registry):
    assert registry.create("acme-org", org_ids=[tenant]).org_ids == [tenant]
    assert registry.get("acme-org").org_ids == [tenant]
    assert SeedOrg(name="acme-org", org_ids=[tenant]).org_ids == [tenant]


@pytest.mark.parametrize("tenant", INVALID)
def test_an_org_is_refused_a_tenant_id_that_breaks_the_fence(tenant, registry, tmp_path):
    with pytest.raises(ValidationError):
        Org(name="acme", org_ids=[tenant])
    with pytest.raises(ValidationError):
        registry.create("acme", org_ids=[tenant])
    assert list(tmp_path.rglob("*.yaml")) == []


@pytest.mark.parametrize("tenant", INVALID)
def test_an_update_is_refused_a_tenant_id_that_breaks_the_fence(tenant, registry):
    registry.create("acme", org_ids=["t-acme-1"])

    with pytest.raises(ValidationError):
        registry.update("acme", org_ids=["t-acme-1", tenant])

    assert registry.get("acme").org_ids == ["t-acme-1"]


@pytest.mark.parametrize("tenant", INVALID)
def test_a_seed_org_is_refused_a_tenant_id_that_breaks_the_fence(tenant):
    with pytest.raises(ValidationError):
        SeedOrg(name="acme", org_ids=[tenant])
