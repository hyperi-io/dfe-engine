#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_org_hunt_tenant_ids.py
#  Purpose:      An org's create, update or delete re-stores the tenant ids of every hunt naming it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST, PUT and DELETE on /api/v1/orgs carry the org's tenant ids into the hunts that name it.

A hunt stores what each customer resolved to when it was saved, because the runner
reads the deploy repo and the org registry is not in it. An org whose ``org_ids``
then changed left its hunts reading the old ids, and the runner moved its watermark
past every row tagged with a new one. The hunts are written before the org, so a
hunt write that fails leaves the org as it was and the request can be sent again.
"""

import pytest

from dfe_engine.api.deps import _registries
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitcrud.log import read_log
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunt_runner.spec_loader import load_specs
from dfe_engine.hunts.deploy_repo import DeployRepoStore
from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry
from dfe_engine.yaml_utils import yaml_load

ORGS = "/api/v1/orgs"
HUNTS = "/api/v1/hunts"
_HUNT = {
    "cron": "*/5 * * * *",
    "rules": ["certutil"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}


@pytest.fixture
def orgs(client, admin_headers) -> None:
    for body in ({"name": "acme", "org_ids": ["acme-1"]}, {"name": "globex"}):
        resp = client.post(ORGS, json=body, headers=admin_headers)
        assert resp.status_code == 201, resp.text


def _create_hunt(client, admin_headers, name: str, customers: list[str]) -> None:
    resp = client.post(
        HUNTS, json={"name": name, **_HUNT, "customers": customers}, headers=admin_headers
    )
    assert resp.status_code == 201, resp.text


def _stored(api_settings, name: str) -> dict:
    return yaml_load(f"{api_settings.hunts.hunt_dir}/{name}.yaml")


class TestTheHuntsFollowTheOrg:
    def test_new_tenant_ids_reach_every_hunt_naming_the_org(
        self, client, admin_headers, api_settings, orgs
    ):
        _create_hunt(client, admin_headers, "windows_hunt", ["acme", "globex"])
        _create_hunt(client, admin_headers, "linux_hunt", ["acme"])

        resp = client.put(
            f"{ORGS}/acme", json={"org_ids": ["acme-1", "acme-2"]}, headers=admin_headers
        )

        assert resp.status_code == 200, resp.text
        assert _stored(api_settings, "windows_hunt")["customer_tenant_ids"] == {
            "acme": ["acme-1", "acme-2"],
            "globex": ["globex"],
        }
        assert _stored(api_settings, "linux_hunt")["customer_tenant_ids"] == {
            "acme": ["acme-1", "acme-2"]
        }

    def test_the_runner_reads_the_new_tenant_ids(self, client, admin_headers, api_settings, orgs):
        from dfe_engine.hunts.rule_model import Rule
        from dfe_engine.hunts.rule_registry import RuleRegistry

        rules = RuleRegistry(
            rules_directory=api_settings.hunts.rules_dir, writable=True, refresh_interval=0
        )
        try:
            rules.save(Rule(rule_id="certutil", name="Certutil", where_clause="x = 1"))
        finally:
            rules.close()
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        assert client.put(
            f"{ORGS}/acme", json={"org_ids": ["acme-2"]}, headers=admin_headers
        ).is_success

        specs = load_specs(api_settings.hunts.hunt_dir, rules_dir=api_settings.hunts.rules_dir)

        (statement,) = specs["windows_hunt"].queries
        assert "_org_id IN ('acme-2')" in statement.sql

    def test_an_org_cleared_of_tenant_ids_reads_its_name(
        self, client, admin_headers, api_settings, orgs
    ):
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])

        assert client.put(f"{ORGS}/acme", json={"org_ids": []}, headers=admin_headers).is_success

        assert _stored(api_settings, "windows_hunt")["customer_tenant_ids"] == {"acme": ["acme"]}

    def test_a_hunt_naming_other_orgs_is_not_rewritten(
        self, client, admin_headers, api_settings, orgs
    ):
        _create_hunt(client, admin_headers, "globex_hunt", ["globex"])
        before = _stored(api_settings, "globex_hunt")

        assert client.put(
            f"{ORGS}/acme", json={"org_ids": ["acme-9"]}, headers=admin_headers
        ).is_success

        assert _stored(api_settings, "globex_hunt") == before

    def test_deleting_an_org_stops_its_hunts_as_unknown_customers(
        self, client, admin_headers, api_settings, orgs
    ):
        """The hunt keeps the name, so it holds its watermark until someone edits it."""
        _create_hunt(client, admin_headers, "windows_hunt", ["acme", "globex"])

        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204

        stored = _stored(api_settings, "windows_hunt")
        assert stored["customers"] == ["acme", "globex"]
        assert stored["customer_tenant_ids"] == {"globex": ["globex"]}
        spec = load_specs(api_settings.hunts.hunt_dir)["windows_hunt"]
        assert spec.unresolved_customers == ["acme"]

    def test_creating_an_org_a_hunt_already_names_resolves_it(
        self, client, admin_headers, api_settings, orgs
    ):
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204

        resp = client.post(
            ORGS, json={"name": "acme", "org_ids": ["acme-7"]}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text
        assert _stored(api_settings, "windows_hunt")["customer_tenant_ids"] == {"acme": ["acme-7"]}

    def test_an_org_cannot_be_renamed_so_no_hunt_follows_a_new_name(
        self, client, admin_headers, api_settings, orgs
    ):
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])

        resp = client.put(f"{ORGS}/acme", json={"name": "acme-new"}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "acme"
        assert client.get(f"{ORGS}/acme-new", headers=admin_headers).status_code == 404
        assert _stored(api_settings, "windows_hunt")["customer_tenant_ids"] == {"acme": ["acme-1"]}


class TestACreateRefusedAfterItsHuntWrite:
    def test_the_hunts_are_put_back_for_the_org_that_won_the_name(
        self, client, app, admin_headers, api_settings, orgs, monkeypatch
    ):
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204
        registry, hunts = app.state.org_registry, _registries["hunt_configs"]
        real = hunts.store_org_tenant_ids

        # A writer that holds nothing, as the startup seed does, lands after the hunt write.
        def racing(*args, **kwargs):
            monkeypatch.setattr(hunts, "store_org_tenant_ids", real)
            stored = real(*args, **kwargs)
            registry.create("acme", org_ids=["acme-seeded"])
            return stored

        monkeypatch.setattr(hunts, "store_org_tenant_ids", racing)

        resp = client.post(
            ORGS, json={"name": "acme", "org_ids": ["acme-7"]}, headers=admin_headers
        )

        assert resp.status_code == 409, resp.text
        assert _stored(api_settings, "windows_hunt")["customer_tenant_ids"] == {
            "acme": ["acme-seeded"]
        }


class _SaveFailingHuntRegistry(HuntConfigRegistry):
    """A hunt registry whose multi-hunt write fails as a full disk fails it."""

    def save_many(self, *_args, **_kwargs):
        """Fail the write, leaving every hunt as it was."""
        raise OSError(28, "No space left on device")


class TestAFailedHuntWriteLeavesTheOrg:
    @pytest.fixture
    def failing_hunts(self, client, admin_headers, api_settings, orgs) -> None:
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        previous = _registries["hunt_configs"]
        previous.close()
        _registries["hunt_configs"] = _SaveFailingHuntRegistry(
            hunts_directory=api_settings.hunts.hunt_dir
        )

    def test_an_update_keeps_the_old_tenant_ids(self, client, admin_headers, failing_hunts):
        resp = client.put(f"{ORGS}/acme", json={"org_ids": ["acme-2"]}, headers=admin_headers)

        assert resp.status_code == 500
        assert client.get(f"{ORGS}/acme", headers=admin_headers).json()["org_ids"] == ["acme-1"]

    def test_a_delete_keeps_the_org(self, client, admin_headers, failing_hunts):
        resp = client.delete(f"{ORGS}/acme", headers=admin_headers)

        assert resp.status_code == 500
        assert client.get(f"{ORGS}/acme", headers=admin_headers).status_code == 200


class _RecordingForge:
    """ForgeProvider seam double: records the PR each write opens."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title})
        return PullRequest(number=7, url="http://forge/pr/7", branch=head)


@pytest.fixture
def deploy_repo(client, tmp_path) -> GitCrud:
    """A real local deploy repo, seeded so main has a base to branch a review from."""
    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    crud.put("hunts", "seed", {**_HUNT, "customers": ["globex"]}, "seed")
    return crud


def _rebind(crud: GitCrud, *, environment: str, mode: str, forge=None) -> None:
    """Point the app's hunt registry at the deploy repo, at one posture."""
    store = DeployRepoStore(crud, "hunts", environment=environment, mode=mode, forge=forge)
    previous = _registries.get("hunt_configs")
    if previous is not None:
        previous.close()
    _registries["hunt_configs"] = HuntConfigRegistry(deploy_repo=store)


class TestTheDeployRepoTakesOneChange:
    def test_one_commit_carries_every_hunt_the_org_change_moves(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _rebind(deploy_repo, environment="dev", mode="solo")
        _create_hunt(client, admin_headers, "windows_hunt", ["acme", "globex"])
        _create_hunt(client, admin_headers, "linux_hunt", ["acme"])
        head = deploy_repo.head_revision()

        resp = client.put(f"{ORGS}/acme", json={"org_ids": ["acme-2"]}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        entries, _ = read_log(deploy_repo, limit=2)
        assert entries[1].sha == head
        assert sorted(entries[0].resources) == ["hunts/linux_hunt", "hunts/windows_hunt"]
        assert entries[0].ctype == "hunt"
        for name in ("windows_hunt", "linux_hunt"):
            assert deploy_repo.get("hunts", name)["customer_tenant_ids"]["acme"] == ["acme-2"]

    def test_an_update_that_leaves_the_tenant_ids_alone_commits_nothing(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _rebind(deploy_repo, environment="dev", mode="solo")
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        head = deploy_repo.head_revision()

        resp = client.put(f"{ORGS}/acme", json={"display_name": "Acme"}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert deploy_repo.head_revision() == head

    def test_production_team_routes_the_change_to_one_review(
        self, client, admin_headers, deploy_repo, orgs
    ):
        """Auto-merge is off, so the hunts change waits on review and the answer says so."""
        _rebind(deploy_repo, environment="dev", mode="solo")
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        _create_hunt(client, admin_headers, "linux_hunt", ["acme"])
        forge = _RecordingForge()
        _rebind(deploy_repo, environment="production", mode="team", forge=forge)
        head = deploy_repo.head_revision()

        resp = client.put(f"{ORGS}/acme", json={"org_ids": ["acme-2"]}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/7"
        assert len(forge.calls) == 1
        assert deploy_repo.head_revision() == head
        assert deploy_repo.get("hunts", "windows_hunt")["customer_tenant_ids"] == {
            "acme": ["acme-1"]
        }

    def test_production_team_with_no_forge_still_keeps_main_unchanged(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _rebind(deploy_repo, environment="dev", mode="solo")
        _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
        _rebind(deploy_repo, environment="production", mode="team")
        head = deploy_repo.head_revision()

        resp = client.delete(f"{ORGS}/acme", headers=admin_headers)

        assert resp.status_code == 204
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert deploy_repo.head_revision() == head


def _under_review(client, admin_headers, deploy_repo) -> None:
    """``windows_hunt`` reads acme's ``acme-1``, and every hunt change now waits on review."""
    _rebind(deploy_repo, environment="dev", mode="solo")
    _create_hunt(client, admin_headers, "windows_hunt", ["acme"])
    _rebind(deploy_repo, environment="production", mode="team")


def _review_branches(crud: GitCrud) -> list[bytes]:
    from dulwich.repo import Repo

    with Repo(str(crud.repo_path)) as repo:
        return sorted(ref for ref in repo.get_refs() if ref.startswith(b"refs/heads/dfe/"))


class TestATenantIdAHuntStillReadsForAnotherOrg:
    """In production+team the org is written while its hunts' rewrite waits on review.

    So a deleted or changed org's ids stay in its hunts on main, or arrive there when a
    review merges, and an org taking them would have its rows read under the old name.
    """

    def test_a_deleted_orgs_ids_still_on_main_are_refused_to_a_new_org(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _under_review(client, admin_headers, deploy_repo)
        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204
        assert deploy_repo.get("hunts", "windows_hunt")["customer_tenant_ids"] == {
            "acme": ["acme-1"]
        }

        resp = client.post(
            ORGS, json={"name": "newcorp", "org_ids": ["acme-1"]}, headers=admin_headers
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "tenant_id_held"
        assert "windows_hunt" in resp.json()["message"]
        assert resp.json()["context"]["held"] == [
            {"hunt": "windows_hunt", "org": "acme", "tenant_ids": ["acme-1"], "branch": None}
        ]
        assert client.get(f"{ORGS}/newcorp", headers=admin_headers).status_code == 404

    def test_an_update_taking_a_held_id_is_refused(self, client, admin_headers, deploy_repo, orgs):
        _under_review(client, admin_headers, deploy_repo)
        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204

        resp = client.put(f"{ORGS}/globex", json={"org_ids": ["acme-1"]}, headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "tenant_id_held"
        assert client.get(f"{ORGS}/globex", headers=admin_headers).json()["org_ids"] == []

    def test_an_id_only_a_pending_review_adds_is_refused(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _under_review(client, admin_headers, deploy_repo)
        assert client.put(
            f"{ORGS}/acme", json={"org_ids": ["acme-1", "acme-2"]}, headers=admin_headers
        ).is_success
        # Main never took acme-2, so this lands at once and leaves the review open.
        assert client.put(
            f"{ORGS}/acme", json={"org_ids": ["acme-1"]}, headers=admin_headers
        ).is_success
        (branch,) = _review_branches(deploy_repo)

        resp = client.post(
            ORGS, json={"name": "newcorp", "org_ids": ["acme-2"]}, headers=admin_headers
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["context"]["held"] == [
            {
                "hunt": "windows_hunt",
                "org": "acme",
                "tenant_ids": ["acme-2"],
                "branch": branch.removeprefix(b"refs/heads/").decode(),
            }
        ]

    def test_deleting_the_review_branch_releases_what_it_carried(
        self, client, admin_headers, deploy_repo, orgs
    ):
        from dulwich import porcelain

        _under_review(client, admin_headers, deploy_repo)
        client.put(f"{ORGS}/acme", json={"org_ids": ["acme-1", "acme-2"]}, headers=admin_headers)
        client.put(f"{ORGS}/acme", json={"org_ids": ["acme-1"]}, headers=admin_headers)
        (branch,) = _review_branches(deploy_repo)
        porcelain.branch_delete(str(deploy_repo.repo_path), branch.removeprefix(b"refs/heads/"))

        resp = client.post(
            ORGS, json={"name": "newcorp", "org_ids": ["acme-2"]}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text

    def test_the_org_itself_may_take_back_its_own_ids(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _under_review(client, admin_headers, deploy_repo)
        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204

        resp = client.post(
            ORGS, json={"name": "acme", "org_ids": ["acme-1"]}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text

    def test_a_review_counts_only_for_the_ids_it_adds(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _under_review(client, admin_headers, deploy_repo)
        client.put(f"{ORGS}/acme", json={"org_ids": ["acme-1", "acme-2"]}, headers=admin_headers)

        held = _registries["hunt_configs"].tenant_id_holders("newcorp", ["acme-1", "acme-2"])

        assert [(h.tenant_ids, h.branch is None) for h in held] == [
            (["acme-1"], True),
            (["acme-2"], False),
        ]

    def test_an_id_no_hunt_reads_is_free_while_a_review_waits(
        self, client, admin_headers, deploy_repo, orgs
    ):
        _under_review(client, admin_headers, deploy_repo)
        assert client.delete(f"{ORGS}/acme", headers=admin_headers).status_code == 204

        resp = client.post(
            ORGS, json={"name": "newcorp", "org_ids": ["newcorp-1"]}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text
