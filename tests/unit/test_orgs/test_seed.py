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


def _hunts(tmp_path, stored: dict):
    from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry

    hunts = HuntConfigRegistry(
        hunts_directory=tmp_path / "hunts", writable=True, refresh_interval=0
    )
    hunts.save(
        "windows_hunt",
        {"cron": "*/5 * * * *", "customers": sorted(stored), "customer_tenant_ids": stored},
    )
    return hunts


_IDS = "customer_tenant_ids"
_HUNT = {"cron": "*/5 * * * *", "rules": ["certutil"]}


def _under_review(tmp_path):
    """A hunt registry on a local deploy repo whose every hunt write waits on review."""
    from dfe_engine.gitcrud import GitCrud, default_registry
    from dfe_engine.gitops.repo import GitopsRepo
    from dfe_engine.hunts.deploy_repo import DeployRepoStore
    from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry

    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    crud.put("hunts", "seed", {**_HUNT, "customers": ["globex"]}, "seed")
    store = DeployRepoStore(crud, "hunts", environment="production", mode="team")
    return HuntConfigRegistry(deploy_repo=store)


class TestASeedBesideTheHunts:
    """The seed creates absent orgs and rewrites no hunt."""

    def test_a_hunt_naming_a_seeded_org_stays_refused_until_it_is_edited(self, registry, tmp_path):
        """A deleted org's hunt holds no ids for it, and the seed adds none, so it reads nothing."""
        from dfe_engine.hunt_runner.spec_loader import load_specs

        _hunts(tmp_path, {"acme": []})

        assert seed_orgs(registry=registry, seeds=[SeedOrg(name="acme")]) == ["acme"]

        spec = load_specs(tmp_path / "hunts")["windows_hunt"]
        assert spec.unresolved_customers == ["acme"]

    def test_a_seed_a_hunt_still_reads_for_another_org_is_not_created(self, registry, tmp_path):
        hunts = _hunts(tmp_path, {"gone": ["t1"]})
        seeds = [SeedOrg(name="newcorp", org_ids=["t1"]), SeedOrg(name="globex")]

        assert seed_orgs(registry=registry, seeds=seeds, hunts=hunts) == ["globex"]

        assert registry.get("newcorp") is None

    def test_a_seed_a_pending_review_gives_another_org_is_not_created(self, registry, tmp_path):
        """In production+team the hunt waits on a review branch, and merging it would read t1."""
        hunts = _under_review(tmp_path)
        hunts.save("windows_hunt", {**_HUNT, "customers": ["gone"], _IDS: {"gone": ["t1"]}})
        seeds = [SeedOrg(name="newcorp", org_ids=["t1"]), SeedOrg(name="globex")]

        assert seed_orgs(registry=registry, seeds=seeds, hunts=hunts) == ["globex"]

        assert registry.get("newcorp") is None

    def test_a_seed_the_forge_cannot_check_waits_for_the_next_boot(
        self, registry, tmp_path, monkeypatch
    ):
        from dfe_engine.gitops.repo import GitopsUnavailableError
        from dfe_engine.orgs import seed as seed_module

        warnings: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            seed_module.logger,
            "warning",
            lambda message, **fields: warnings.append((message, fields)),
        )
        answers: list[Exception | list] = [GitopsUnavailableError("forge down"), []]

        class _Hunts:
            def tenant_id_holders(self, *_args, **_kwargs) -> list:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer

        seeds = [SeedOrg(name="newcorp", org_ids=["t1"]), SeedOrg(name="globex")]

        assert seed_orgs(registry=registry, seeds=seeds, hunts=_Hunts()) == ["globex"]

        assert registry.get("newcorp") is None
        [(message, fields)] = warnings
        assert "not seeded" in message
        assert fields["org"] == "newcorp"
