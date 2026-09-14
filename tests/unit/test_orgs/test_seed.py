#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_seed.py
#  Purpose:      Tests for seeding organisations from settings
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.orgs.seed import seed_orgs
from dfe_engine.settings import SeedOrg
from dfe_engine.state_machines.setup import SETUP_MACHINE, STEP_ORGANISATIONS, SetupContext


@pytest.fixture
def registry(tmp_path):
    return OrgRegistry(tmp_path / "orgs")


class TestSeedOrgs:
    def test_creates_absent_org_with_its_fields(self, registry):
        seeds = [SeedOrg(display_name="Acme Corp", name="acme", org_ids=["acme", "acme-sub"])]

        created = seed_orgs(registry=registry, seeds=seeds)

        assert created == ["acme"]
        org = registry.get("acme")
        assert org is not None
        assert org.display_name == "Acme Corp"
        assert org.org_ids == ["acme", "acme-sub"]

    def test_leaves_existing_org_untouched(self, registry):
        registry.create(display_name="Edited in console", name="acme", org_ids=["edited"])
        seeds = [SeedOrg(display_name="From config", name="acme", org_ids=["acme"])]

        created = seed_orgs(registry=registry, seeds=seeds)

        assert created == []
        org = registry.get("acme")
        assert org is not None
        assert org.display_name == "Edited in console"
        assert org.org_ids == ["edited"]

    def test_second_run_creates_nothing(self, registry):
        seeds = [SeedOrg(name="acme"), SeedOrg(name="globex")]

        assert seed_orgs(registry=registry, seeds=seeds) == ["acme", "globex"]
        assert seed_orgs(registry=registry, seeds=seeds) == []
        assert [org.name for org in registry.list()] == ["acme", "globex"]

    def test_never_deletes_an_org_the_seed_does_not_name(self, registry):
        registry.create(name="made-in-console")

        seed_orgs(registry=registry, seeds=[SeedOrg(name="acme")])

        assert [org.name for org in registry.list()] == ["acme", "made-in-console"]

    def test_empty_seed_list_creates_nothing(self, registry):
        assert seed_orgs(registry=registry, seeds=[]) == []
        assert registry.list() == []

    def test_seeded_org_completes_the_organisations_setup_step(self, registry):
        seed_orgs(registry=registry, seeds=[SeedOrg(name="acme")])

        state = SETUP_MACHINE.evaluate(SetupContext(org_registry=registry))

        assert STEP_ORGANISATIONS in state.completed_steps
        assert STEP_ORGANISATIONS not in state.pending_steps
