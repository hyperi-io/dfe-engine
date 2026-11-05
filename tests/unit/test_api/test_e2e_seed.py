#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_e2e_seed.py
#  Purpose:      e2e Seed base + Account seeder (test-env gated)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest

from dfe_engine.appmgmt import instances
from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.bootstrap import bootstrap_auth
from dfe_engine.auth.groups import GroupStore
from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.settings import DFESettings
from tests.support.present import present


def _stores(
    tmp_path: Path,
) -> tuple[AccountStore, GroupStore, OrgRegistry]:
    """Stores as the running app hands them to the seeders.

    ``api/app.py``'s lifespan always runs ``bootstrap_auth`` before the e2e
    routes can be called, so the default groups and the break-glass admin are
    already there. Going through the real bootstrap (rather than hand-built
    empty stores) is what keeps the seeders' group names pinned to the ones
    startup actually creates -- ``_ensure_group`` raises on any drift.
    """
    account_store, group_store, *_ = bootstrap_auth(tmp_path / "config" / "auth")
    return account_store, group_store, OrgRegistry(tmp_path / "orgs")


def _crud(tmp_path: Path) -> GitCrud:
    """GitCrud over a fresh local (no-remote) deploy repo."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _deploy(gc: GitCrud, service: str, instance: str) -> instances.AppInstance:
    """Commit a minimal overlay for one instance, as a seeder would."""
    app = instances.instance_of(service, instance)
    gc.put(
        instances.HELMVARS_CLASS,
        app.overlay_name,
        instances.initial_overlay(app),
        "test",
        message="test: deploy",
    )
    return app


def test_seed_refuses_when_env_is_not_test(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "dev")
    accounts, groups, orgs = _stores(tmp_path)
    with pytest.raises(PermissionError, match="DFE_ENGINE_ENV=test"):
        Seed(account_store=accounts, group_store=groups, org_registry=orgs)


def test_seed_refuses_explicit_non_test_env_even_if_environ_is_test(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    with pytest.raises(PermissionError, match="DFE_ENGINE_ENV=test"):
        Seed(account_store=accounts, group_store=groups, org_registry=orgs, env="production")


def test_seed_allows_test_env_from_environ(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    Seed(account_store=accounts, group_store=groups, org_registry=orgs)


def test_seed_allows_explicit_test_env_when_environ_is_dev(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "dev")
    accounts, groups, orgs = _stores(tmp_path)
    Seed(account_store=accounts, group_store=groups, org_registry=orgs, env="test")


def test_seed_dispatches_dfe_admin_user_to_accounts(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    # The seeder adds a well-known e2e account alongside the bootstrap
    # break-glass admin; it must not rename or clobber that admin.
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_dfe_admin_user") is True
    assert seeder.seed_static("unknown") is False
    user = accounts.get("dfe_admin")
    assert user is not None
    assert "dfe-admins" in user.groups
    assert accounts.verify_password("dfe_admin", "changeme")
    assert "dfe_admin" in present(groups.get("dfe-admins")).members
    # The bootstrap-owned break-glass account is untouched by the seed.
    assert accounts.verify_password("admin", "changeme")


def test_account_ensure_admin_joins_the_bootstrapped_group(tmp_path, monkeypatch):
    """The seeder attaches membership; bootstrap owns the group itself."""
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_dfe_admin_user(name="playwright", password="e2e-secret") is True
    user = accounts.get("playwright")
    assert user is not None
    assert user.enabled is True
    assert "dfe-admins" in user.groups
    assert accounts.verify_password("playwright", "e2e-secret")
    admins = groups.get("dfe-admins")
    assert admins is not None
    assert admins.roles == ["admin"]
    assert "playwright" in admins.members


def test_account_ensure_admin_resets_existing(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_dfe_admin_user(name="dfe_admin", password="first-pass") is True
    assert seeder.seed_dfe_admin_user(name="dfe_admin", password="second-pass") is False
    assert accounts.verify_password("dfe_admin", "second-pass")
    assert not accounts.verify_password("dfe_admin", "first-pass")


def test_account_ensure_reuses_private_upsert_for_other_groups(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)
    seeder._ensure_group("dfe-analysts")

    assert seeder._ensure("analyst", "analyst-pw", groups=["dfe-analysts"]) is True
    user = accounts.get("analyst")
    assert user is not None
    assert "dfe-analysts" in user.groups
    assert "analyst" in present(groups.get("dfe-analysts")).members
    assert accounts.verify_password("analyst", "analyst-pw")


def test_organisation_seed_creates_default(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.organisations import Organisations

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Organisations(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_organisation() is True
    org = orgs.get("organisation")
    assert org is not None
    assert org.enabled is True
    assert org.display_name == "organisation"
    assert org.org_ids == ["organisation"]


def test_organisation_seed_resets_existing(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed.organisations import Organisations

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Organisations(account_store=accounts, group_store=groups, org_registry=orgs)

    orgs.create("organisation", org_ids=[], display_name="stale")
    orgs.update("organisation", enabled=False)

    assert seeder.seed_organisation(display_name="Organisation") is False
    org = orgs.get("organisation")
    assert org is not None
    assert org.enabled is True
    assert org.display_name == "Organisation"
    assert org.org_ids == ["organisation"]


def _hunts(tmp_path: Path, customers: list[str], stored: dict | None = None):
    """A hunt registry holding ``windows_hunt``, naming ``customers`` with ``stored`` ids."""
    from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry

    hunts = HuntConfigRegistry(
        hunts_directory=tmp_path / "hunts", writable=True, refresh_interval=0
    )
    hunts.save(
        "windows_hunt",
        {"cron": "*/5 * * * *", "customers": customers, "customer_tenant_ids": stored or {}},
    )
    return hunts


def _organisations(tmp_path: Path, hunts):
    from dfe_engine.api.e2e.seed.organisations import Organisations

    accounts, groups, orgs = _stores(tmp_path)
    seeder = Organisations(
        account_store=accounts, group_store=groups, org_registry=orgs, hunt_registry=hunts
    )
    return seeder, orgs


class TestTheOrganisationSeedMovesTheHuntsNamingIt:
    """The seeder writes orgs as the org routes do, so the hunts naming one follow it."""

    def test_a_seeded_org_is_stored_in_the_hunts_naming_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DFE_ENGINE_ENV", "test")
        hunts = _hunts(tmp_path, ["organisation"])
        seeder, _orgs = _organisations(tmp_path, hunts)

        assert seeder.seed_organisation() is True

        assert hunts.get("windows_hunt")["customer_tenant_ids"] == {
            "organisation": ["organisation"]
        }

    def test_a_reset_org_restores_its_ids_in_the_hunts_naming_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DFE_ENGINE_ENV", "test")
        hunts = _hunts(tmp_path, ["organisation"], {"organisation": ["stale"]})
        seeder, orgs = _organisations(tmp_path, hunts)
        orgs.create("organisation", org_ids=["stale"])

        assert seeder.seed_organisation() is False

        assert hunts.get("windows_hunt")["customer_tenant_ids"] == {
            "organisation": ["stale", "organisation"]
        }

    def test_deleting_every_org_takes_each_out_of_its_hunts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DFE_ENGINE_ENV", "test")
        hunts = _hunts(tmp_path, ["organisation"])
        seeder, orgs = _organisations(tmp_path, hunts)
        seeder.seed_organisation()

        seeder.delete_all()

        assert orgs.list() == []
        assert hunts.get("windows_hunt")["customer_tenant_ids"] == {}

    def test_a_seed_taking_an_id_a_hunt_reads_for_another_org_is_refused(
        self, tmp_path, monkeypatch
    ):
        from scalo.api import ProblemError

        monkeypatch.setenv("DFE_ENGINE_ENV", "test")
        hunts = _hunts(tmp_path, ["gone"], {"gone": ["organisation"]})
        seeder, orgs = _organisations(tmp_path, hunts)

        with pytest.raises(ProblemError) as refused:
            seeder.seed_organisation()

        assert refused.value.status == 409
        assert orgs.get("organisation") is None


def test_reset_all_recreates_break_glass_admin(tmp_path, monkeypatch):
    """reset_all must leave a loginable break-glass admin (delete_all removes every account)."""
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.bootstrap import admin_account_name, admin_account_password

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")

    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    seeder.accounts.seed_dfe_admin_user(name="extra_user")
    seeder.organisations.seed_organisation()
    assert len(accounts.list()) >= 2

    assert seeder.seed_static("reset_all") is True
    admin_name = admin_account_name()
    remaining = accounts.list()
    assert len(remaining) == 1
    assert remaining[0].username == admin_name
    assert remaining[0].email == "admin@dfe.local"
    assert accounts.verify_password(admin_name, admin_account_password())


def test_seed_setup_complete_seeds_organisation(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_setup_complete") is True
    assert accounts.get("admin") is not None
    assert accounts.verify_password("admin", "already_reset")
    assert not accounts.verify_password("admin", "changeme")
    assert accounts.get("initial_user") is not None
    assert orgs.get("organisation") is not None


def test_searchable_organisations_are_local_and_fenced(tmp_path, monkeypatch):
    """Fifty orgs, each holding only its own tenant id. A second call writes nothing."""
    from dfe_engine.api.e2e.seed.organisations import (
        Organisations,
        searchable_organisation_names,
    )
    from dfe_engine.orgs.models import DEFAULT_ORG_NAME

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    orgs.create(DEFAULT_ORG_NAME, org_ids=["default"], display_name="Default")
    seeder = Organisations(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_searchable_organisations() is True
    assert seeder.seed_searchable_organisations() is False
    names = searchable_organisation_names()
    assert len(names) == 50
    for num, name in enumerate(names):
        org = orgs.get(name)
        assert org is not None
        assert org.display_name == f"Searchable Organisation {num}"
        assert org.org_ids == [name]
        assert org.enabled is True
    default = orgs.get(DEFAULT_ORG_NAME)
    assert default is not None
    assert default.org_ids == ["default"]

    seeder.delete_all()
    assert orgs.get(DEFAULT_ORG_NAME) is not None
    assert orgs.get(names[0]) is None


def test_seed_organisation_dispatches_to_the_organisation_seeder(tmp_path, monkeypatch):
    """The script is offered in the request Literal, so it has to reach a seeder."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.seed_static("seed_organisation") is True
    assert orgs.get("organisation") is not None


def test_an_app_seed_without_a_deploy_repo_names_the_setting(tmp_path, monkeypatch):
    """A misconfigured e2e-server must say what is missing, not seed nothing quietly."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    with pytest.raises(RuntimeError, match="DFE_ENGINE_GITOPS_ENABLED"):
        seeder.seed_static("seed_app_scaling_state")


def test_reset_all_is_safe_without_a_deploy_repo(tmp_path, monkeypatch):
    """The auth-only e2e specs run on a process with gitops off; reset must not raise."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    seeder.accounts.seed_dfe_admin_user()
    assert seeder.seed_static("reset_all") is True


def test_reset_all_keeps_a_single_deployment_apps_values(tmp_path, monkeypatch):
    """dfe-engine's own overlay is standing infra no seeder here writes; a source resets."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    gc = _crud(tmp_path)
    single = _deploy(gc, "dfe-engine", "default")
    per_source = _deploy(gc, "dfe-transform-vrl", "crowdstrike")
    seeder = Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=orgs,
        gitcrud=gc,
        settings=DFESettings(env="test", transport={"default": "bus"}),
    )

    assert seeder.seed_static("reset_all") is True

    assert instances.exists(gc, single)
    assert not instances.exists(gc, per_source)


def test_reset_all_keeps_the_deployers_common_infra_overlay(tmp_path, monkeypatch):
    """The stack-wide infra baseline predates any run; a per-chart dial does not."""
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    gc = _crud(tmp_path)
    gc.put("infravars", "common", {"clickhouse": {"replicas": 3}}, "test", message="test: deploy")
    gc.put("infravars", "clickhouse", {"replicas": 5}, "test", message="test: dial")
    seeder = Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=orgs,
        gitcrud=gc,
        settings=DFESettings(env="test", transport={"default": "bus"}),
    )

    assert seeder.seed_static("reset_all") is True

    assert "common" in gc.list("infravars")
    assert "clickhouse" not in gc.list("infravars")


def _pool_resets(manager, service: str, action: str) -> float:
    """The pool-reset counter for one service and action, read off the real exposition."""
    from prometheus_client.parser import text_string_to_metric_families

    from dfe_engine.api.e2e.seed.metrics import POOL_RESETS

    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == POOL_RESETS and sample.labels == {
                "service": service,
                "action": action,
            }:
                return sample.value
    return 0.0


def _pool_seeder(tmp_path: Path, gc: GitCrud, manager=None):
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.api.e2e.seed.metrics import SeedMetrics

    accounts, groups, orgs = _stores(tmp_path)
    return Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=orgs,
        gitcrud=gc,
        settings=DFESettings(env="test", transport={"default": "bus"}),
        metrics=SeedMetrics(manager),
    )


def test_reset_all_restores_a_marked_pool_and_removes_an_unmarked_one(tmp_path, monkeypatch):
    """The marker is the deployer's record, so a pool it does not name came from a seed."""
    from scalo.metrics import create_metrics

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    gc = _crud(tmp_path)
    gc.repo.publish({".seeded-apps": "dfe-receiver\n"}, "test: marker")
    receiver = _deploy(gc, "dfe-receiver", "default")
    loader = _deploy(gc, "dfe-loader", "default")
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    seeder = _pool_seeder(tmp_path, gc, manager)

    assert seeder.seed_static("reset_all") is True

    assert instances.exists(gc, receiver)
    assert not instances.exists(gc, loader)
    assert _pool_resets(manager, "dfe-receiver", "restored") == 1
    assert _pool_resets(manager, "dfe-loader", "removed") == 1

    gc.delete(instances.HELMVARS_CLASS, receiver.overlay_name, "test", message="test: removed")
    assert seeder.seed_static("reset_all") is True

    assert instances.exists(gc, receiver)
    assert _pool_resets(manager, "dfe-receiver", "recreated") == 1


def test_reset_all_keeps_the_identity_a_compose_deployer_wrote(tmp_path, monkeypatch):
    """The engine's own seed writes an OTel name, which the reset keeps as it drops the dials."""
    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    gc = _crud(tmp_path)
    gc.repo.publish({".seeded-apps": "dfe-receiver\n"}, "test: marker")
    receiver = instances.instance_of("dfe-receiver", "default")
    doc = instances.initial_overlay(receiver)
    doc["keda"] = {"enabled": True, "minReplicaCount": 2}
    gc.put(instances.HELMVARS_CLASS, receiver.overlay_name, doc, "test", message="test: dials")

    assert _pool_seeder(tmp_path, gc).seed_static("reset_all") is True

    assert instances.read_overlay(gc, receiver) == instances.initial_overlay(receiver)


def test_reset_all_is_one_commit(tmp_path, monkeypatch):
    from dulwich.repo import Repo

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    gc = _crud(tmp_path)
    gc.repo.publish({".seeded-apps": "dfe-receiver\n"}, "test: marker")
    _deploy(gc, "dfe-receiver", "default")
    _deploy(gc, "dfe-loader", "default")
    _deploy(gc, "dfe-transform-vrl", "crowdstrike")
    gc.put("infravars", "clickhouse", {"replicas": 5}, "test", message="test: dial")

    def commits() -> int:
        with Repo(str(gc.repo_path)) as repo:
            return sum(1 for _ in repo.get_walker())

    before = commits()
    assert _pool_seeder(tmp_path, gc).seed_static("reset_all") is True

    assert commits() == before + 1


def test_seed_metrics_without_a_backend_record_nothing():
    from dfe_engine.api.e2e.seed.metrics import SeedMetrics

    metrics = SeedMetrics()

    assert metrics.enabled is False
    metrics.pool_reset("dfe-receiver", "recreated")


def test_seeders_never_create_groups_themselves(tmp_path, monkeypatch):
    """Groups are startup-bootstrap territory: a seeder refuses an unknown one.

    This is the guard that keeps `make e2e-server` honest -- the seeders may
    only join groups `dfe-engine run` already created, so an e2e process can
    never end up with a roster the product bootstrap would not produce.
    """
    from dfe_engine.api.e2e.seed.accounts import Accounts

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Accounts(account_store=accounts, group_store=groups, org_registry=orgs)

    with pytest.raises(ValueError, match="dfe-nonesuch does not exist"):
        seeder._ensure_group("dfe-nonesuch")
    assert groups.get("dfe-nonesuch") is None


def test_searchable_accounts_stay_in_the_account_store(tmp_path, monkeypatch):
    """Fifty ordinary accounts and fifty blocked ones, searchable, and not published anywhere else."""
    from dfe_engine.api.conventions import apply_search
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)

    assert seeder.accounts.seed_searchable_accounts() is True
    assert seeder.accounts.seed_searchable_accounts() is False
    assert seeder.accounts.seed_searchable_blocked_accounts() is True
    assert seeder.accounts.seed_searchable_blocked_accounts() is False

    rows = [
        {
            "username": account.username,
            "name": account.name,
            "email": account.email,
            "blocked": account.blocked,
        }
        for account in accounts.list()
    ]
    found = apply_search(rows, "searchable", ["username", "name", "email"])
    assert len(found) == 100
    assert sum(1 for row in found if row["blocked"]) == 50
    blocked = [row for row in found if row["username"].startswith("searchable-blocked-")]
    ordinary = [row for row in found if row["username"].startswith("searchable-account-")]
    assert len(blocked) == 50
    assert all(row["blocked"] for row in blocked)
    assert len(ordinary) == 50
    assert all(not row["blocked"] for row in ordinary)
    viewers = present(groups.get("dfe-viewers"))
    assert "searchable-account-0" in viewers.members
    assert "searchable-blocked-0" in viewers.members

    seeder.accounts.delete_all()
    left = [account.username for account in accounts.list() if "searchable" in account.username]
    assert left == []


def test_searchable_api_keys_stay_in_the_key_store(tmp_path, monkeypatch):
    """Fifty keys the list can search, kept in the key store and removed by name."""
    from dfe_engine.api.conventions import apply_search
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.bootstrap import bootstrap_auth

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    account_store, group_store, api_key_store, *_ = bootstrap_auth(tmp_path / "config" / "auth")
    seeder = Seed(
        account_store=account_store,
        group_store=group_store,
        org_registry=OrgRegistry(tmp_path / "orgs"),
        api_key_store=api_key_store,
    )

    assert seeder.accounts.seed_searchable_api_keys() is True
    assert seeder.accounts.seed_searchable_api_keys() is False
    rows = [{"name": key.name, "description": key.description} for key in api_key_store.list()]
    found = apply_search(rows, "searchable", ["name", "description"])
    assert len(found) == 50
    assert all(key.key_hash.startswith("sha384:") for key in api_key_store.list())
    assert all(not key.is_expired() for key in api_key_store.list())

    seeder.accounts.delete_searchable_api_keys()
    assert api_key_store.list() == []


def test_searchable_api_keys_refuse_without_a_store(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)
    with pytest.raises(RuntimeError, match="API key store"):
        seeder.accounts.seed_searchable_api_keys()


def test_searchable_groups_and_roles_stay_beside_the_builtins(tmp_path, monkeypatch):
    """Fifty groups and fifty custom roles, searchable, without touching the built-ins."""
    from dfe_engine.api.conventions import apply_search
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.bootstrap import bootstrap_auth

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    account_store, group_store, _api_keys, role_store, *_ = bootstrap_auth(
        tmp_path / "config" / "auth"
    )
    core_before = [role.name for role in role_store.list() if role.resource_type == "core"]
    seeder = Seed(
        account_store=account_store,
        group_store=group_store,
        org_registry=OrgRegistry(tmp_path / "orgs"),
        role_store=role_store,
    )

    assert seeder.accounts.seed_searchable_groups() is True
    assert seeder.accounts.seed_searchable_groups() is False
    assert seeder.accounts.seed_searchable_roles() is True
    assert seeder.accounts.seed_searchable_roles() is False

    groups = apply_search(
        [{"name": group.name, "description": group.description} for group in group_store.list()],
        "searchable",
        ["name", "description"],
    )
    roles = apply_search(
        [{"name": role.name, "description": role.description} for role in role_store.list()],
        "searchable",
        ["name", "description"],
    )
    assert len(groups) == 50
    assert len(roles) == 50
    seeded_roles = [role for role in role_store.list() if role.name.startswith("searchable-role-")]
    assert all(role.resource_type == "custom" and role.permissions == [] for role in seeded_roles)

    seeder.accounts.delete_searchable_groups()
    seeder.accounts.delete_searchable_roles()
    assert group_store.get("dfe-viewers") is not None
    assert group_store.get("searchable-group-0") is None
    assert role_store.get("searchable-role-0") is None
    assert [role.name for role in role_store.list() if role.resource_type == "core"] == core_before


def test_searchable_roles_refuse_without_a_store(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)
    with pytest.raises(RuntimeError, match="role store"):
        seeder.accounts.seed_searchable_roles()


def test_searchable_oidc_providers_stay_disabled_in_the_registry(tmp_path, monkeypatch):
    """Fifty disabled providers the list can search, with no issuer and no secret."""
    from dfe_engine.api.conventions import apply_search
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    registry = OIDCProviderRegistry(tmp_path / "oidc-providers")
    seeder = Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=orgs,
        oidc_registry=registry,
    )

    assert seeder.accounts.seed_searchable_oidc_providers() is True
    assert seeder.accounts.seed_searchable_oidc_providers() is False
    rows = [
        {"name": name, "display_name": provider.display_name} for name, provider in registry.list()
    ]
    found = apply_search(rows, "searchable", ["name", "display_name"])
    assert len(found) == 50
    providers = [provider for _, provider in registry.list()]
    assert all(not provider.enabled for provider in providers)
    assert all(provider.issuer == "" for provider in providers)
    assert all(provider.client_secret_env == "" for provider in providers)
    assert all(provider.client_secret_path == "" for provider in providers)
    assert all(provider.groups.mode != "api" for provider in providers)

    seeder.accounts.delete_searchable_oidc_providers()
    assert registry.list() == []


def test_searchable_oidc_providers_refuse_without_a_registry(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    seeder = Seed(account_store=accounts, group_store=groups, org_registry=orgs)
    with pytest.raises(RuntimeError, match="provider registry"):
        seeder.accounts.seed_searchable_oidc_providers()


def test_searchable_oidc_users_attach_to_the_first_provider(tmp_path, monkeypatch):
    """Ordinary accounts take the first provider's stamp. Recovery accounts stay local."""
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.oidc.models import OIDCProvider
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    registry = OIDCProviderRegistry(tmp_path / "oidc-providers")
    registry.create(
        "searchable-oidc-0", OIDCProvider(enabled=False, display_name="Searchable Provider 0")
    )
    for name in ("searchable-account-0", "searchable-blocked-0"):
        accounts.create(name, "changeme", groups=["dfe-viewers"])
    seeder = Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=orgs,
        oidc_registry=registry,
    )

    assert seeder.accounts.seed_searchable_oidc_users() is True
    assert seeder.accounts.seed_searchable_oidc_users() is False
    for name in ("searchable-account-0", "searchable-blocked-0"):
        account = present(accounts.get(name))
        assert account.oidc_id == "searchable-oidc-0"
        assert account.source_provider == "searchable-oidc-0"
        assert account.external is False
    admin = present(accounts.get("admin"))
    assert admin.oidc_id == ""
    assert admin.source_provider == ""


def test_searchable_oidc_users_refuse_without_the_first_provider(tmp_path, monkeypatch):
    from dfe_engine.api.e2e.seed import Seed
    from dfe_engine.auth.oidc.registry import OIDCProviderRegistry

    monkeypatch.setenv("DFE_ENGINE_ENV", "test")
    accounts, groups, orgs = _stores(tmp_path)
    accounts.create("searchable-account-0", "changeme", groups=["dfe-viewers"])
    seeder = Seed(
        account_store=accounts,
        group_store=groups,
        org_registry=orgs,
        oidc_registry=OIDCProviderRegistry(tmp_path / "oidc-providers"),
    )
    with pytest.raises(RuntimeError, match="searchable-oidc-0"):
        seeder.accounts.seed_searchable_oidc_users()
    assert present(accounts.get("searchable-account-0")).oidc_id == ""


def test_no_seeded_match_rule_carries_the_json_column_prefix():
    """A seeded rule the receiver can never match is a fixture that proves nothing.

    dfe-receiver splits a match field on '.' and walks the RAW payload
    (src/routing/mod.rs, extract_field_cow), so a '_json.' prefix becomes a first
    segment no record has. The prefix is meaningful on the schema side, where it
    names a subcolumn - never in a match rule, and an AST scan is what stops it
    coming back through any seeder.
    """
    import ast
    from pathlib import Path

    from dfe_engine.api.e2e import seed

    offenders: list[str] = []
    for module in sorted(Path(seed.__file__).parent.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        offenders += [
            f"{module.name}:{node.lineno} {node.value}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("_json.")
        ]
    assert offenders == [], f"seeded match fields carry the JSON-column prefix: {offenders}"
