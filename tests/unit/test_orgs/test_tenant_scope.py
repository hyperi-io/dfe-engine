#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_tenant_scope.py
#  Purpose:      One rule resolves an org marker to its org, by name or by tenant id
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``resolve_orgs``, ``tenant_ids_for`` and ``colliding_org`` over org markers.

A marker is an org's name or one of its tenant ids. Either names that org and
maps to exactly that org's tenant ids; anything that does not name exactly one
org maps to nothing. ``colliding_org`` is the write-time guard: a write may not
plant a marker another org already owns.
"""

import pytest

from dfe_engine.orgs.models import Org
from dfe_engine.orgs.tenant_scope import colliding_org, resolve_orgs, tenant_ids_for

ACME = Org(name="acme", org_ids=["t-acme-1", "t-acme-2"])
BETA = Org(name="beta", org_ids=[])
ORGS = [ACME, BETA]


def _names(orgs: list[Org]) -> list[str]:
    return [org.name for org in orgs]


@pytest.mark.parametrize("marker", ["acme", "t-acme-1", "t-acme-2"])
def test_a_name_and_each_tenant_id_name_the_same_org(marker):
    assert _names(resolve_orgs([marker], ORGS)) == ["acme"]
    assert tenant_ids_for([marker], ORGS) == ["t-acme-1", "t-acme-2"]


def test_an_org_declaring_no_tenant_ids_is_named_by_its_name():
    assert _names(resolve_orgs(["beta"], ORGS)) == ["beta"]
    assert tenant_ids_for(["beta"], ORGS) == ["beta"]


def test_markers_naming_one_org_twice_resolve_it_once():
    assert _names(resolve_orgs(["t-acme-2", "acme", "t-acme-1"], ORGS)) == ["acme"]


def test_markers_naming_two_orgs_resolve_both():
    assert _names(resolve_orgs(["beta", "t-acme-1"], ORGS)) == ["acme", "beta"]
    assert tenant_ids_for(["beta", "t-acme-1"], ORGS) == ["beta", "t-acme-1", "t-acme-2"]


@pytest.mark.parametrize("markers", [[], ["nope"], ["ACME"], ["t-acme"]])
def test_a_marker_no_org_declares_names_nothing(markers):
    assert resolve_orgs(markers, ORGS) == []
    assert tenant_ids_for(markers, ORGS) == []


def test_an_unknown_marker_beside_a_known_one_adds_nothing():
    assert tenant_ids_for(["nope", "acme"], ORGS) == ["t-acme-1", "t-acme-2"]


def test_a_tenant_id_two_orgs_declare_names_neither():
    gamma = Org(name="gamma", org_ids=["t-shared"])
    delta = Org(name="delta", org_ids=["t-shared", "t-delta"])

    assert resolve_orgs(["t-shared"], [gamma, delta]) == []
    assert tenant_ids_for(["t-shared"], [gamma, delta]) == []


def test_a_name_wins_over_another_orgs_tenant_id():
    named = Org(name="t-acme-1", org_ids=["t-other"])

    assert _names(resolve_orgs(["t-acme-1"], [ACME, named])) == ["t-acme-1"]
    assert tenant_ids_for(["t-acme-1"], [ACME, named]) == ["t-other"]


def test_no_registered_orgs_resolve_nothing():
    assert resolve_orgs(["acme"], []) == []


# ---------------------------------------------------------------------------
# colliding_org
# ---------------------------------------------------------------------------


def test_a_new_tenant_id_already_declared_by_another_org_collides():
    other = colliding_org("charlie", ["t-acme-1"], ORGS)

    assert other is not None
    assert other.name == "acme"


def test_a_new_tenant_id_equal_to_another_orgs_name_collides():
    other = colliding_org("charlie", ["beta"], ORGS)

    assert other is not None
    assert other.name == "beta"


def test_a_new_name_equal_to_another_orgs_tenant_id_collides():
    other = colliding_org("t-acme-1", [], ORGS)

    assert other is not None
    assert other.name == "acme"


def test_an_org_keeping_its_own_name_among_its_own_tenant_ids_is_not_a_collision():
    assert colliding_org("acme", ["acme", "t-acme-1", "t-acme-2"], ORGS) is None


def test_no_overlap_is_not_a_collision():
    assert colliding_org("charlie", ["t-charlie-1"], ORGS) is None


def test_updating_an_org_is_checked_against_everyone_else_but_itself():
    # acme's own stored record is in the list, as it is on a real update, and is skipped.
    assert colliding_org("acme", ["t-acme-1", "t-new"], ORGS) is None
    assert colliding_org("acme", ["beta"], ORGS) is not None
