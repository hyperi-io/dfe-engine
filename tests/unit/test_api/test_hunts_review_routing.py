#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_review_routing.py
#  Purpose:      Hunt and rule writes report a review branch instead of claiming success
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A production+team hunt or rule write lands on a review branch, and says so.

Without the signal a POST answers 201 and a DELETE answers 204 while the change
sits unmerged, so the hunt runner keeps running exactly what the operator was told
had changed. The header contract is the one governance's fixed-body 201/204
endpoints already use.
"""

from __future__ import annotations

import pytest

from dfe_engine.api.deps import _registries
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunts.deploy_repo import DeployRepoStore
from dfe_engine.hunts.hunt_config_registry import HuntConfigRegistry
from dfe_engine.hunts.rule_registry import RuleRegistry

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "rules": ["certutil"],
    "global_source_table_name": "dfe.default",
    "global_target_table_name": "dfe.detection",
}


class _RecordingForge:
    """ForgeProvider seam double: records the PR the write opens."""

    def __init__(self, url: str = "http://forge/pr/11") -> None:
        self.calls: list[dict] = []
        self._url = url

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequest(number=11, url=self._url, branch=head)


def _store(crud, cls_name, *, environment, mode, forge=None) -> DeployRepoStore:
    return DeployRepoStore(crud, cls_name, environment=environment, mode=mode, forge=forge)


def _rebind(crud, *, environment: str, mode: str, forge=None) -> None:
    """Point the app's hunt and rule registries at the deploy repo, at one posture.

    Closes whatever it replaces: the lifespan built directory-backed registries with
    background refresh threads, and the shutdown only closes what is in the dict.
    """
    replacements = {
        "hunt_configs": HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment=environment, mode=mode, forge=forge)
        ),
        "rules": RuleRegistry(
            deploy_repo=_store(crud, "rules", environment=environment, mode=mode, forge=forge)
        ),
    }
    for key, registry in replacements.items():
        previous = _registries.get(key)
        if previous is not None:
            previous.close()
        _registries[key] = registry


@pytest.fixture
def deploy_repo(client, api_settings, tmp_path):
    """A real local deploy repo behind the running app's hunt and rule registries.

    ``client`` first: the app lifespan builds the directory-backed registries, and
    these replace them. The rule template lives where HuntValidator looks for it, so
    a hunt create passes validation rather than 422-ing before it reaches git.
    """
    (tmp_path / "rules" / "certutil.jinja2").write_text(
        "SELECT * FROM {{ source_table }} WHERE {{ window }}", encoding="utf-8"
    )
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    crud = GitCrud(repo, default_registry())
    # An empty repo has no base to branch from, so main carries a first commit.
    crud.put("hunts", "seed", dict(_HUNT), "seed")
    return crud


def _rule_payload(name: str = "certutil") -> dict:
    return {
        "name": name,
        "display_name": "Certutil Abuse",
        "severity": "high",
        "user_sql": "SELECT * FROM default.events WHERE severity = 'high'",
    }


class TestDevPostureIsUnchanged:
    def test_a_hunt_create_in_dev_carries_no_review_header(
        self, client, admin_headers, deploy_repo
    ):
        _rebind(deploy_repo, environment="dev", mode="team")
        resp = client.post(
            "/api/v1/hunts", json={"name": "windows_hunt", **_HUNT}, headers=admin_headers
        )
        assert resp.status_code == 201, resp.text
        assert "X-DFE-Review-Required" not in resp.headers
        assert (deploy_repo.repo_path / "config" / "hunts" / "windows_hunt.yaml").is_file()


class TestProductionTeamWritesReportReview:
    def test_a_hunt_create_says_review_required_and_names_the_pr(
        self, client, admin_headers, deploy_repo
    ):
        forge = _RecordingForge(url="http://forge/pr/42")
        _rebind(deploy_repo, environment="production", mode="team", forge=forge)

        resp = client.post(
            "/api/v1/hunts", json={"name": "windows_hunt", **_HUNT}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/42"
        # main never got the file, so the git-sync sidecar does not serve it
        assert not (deploy_repo.repo_path / "config" / "hunts" / "windows_hunt.yaml").exists()

    def test_a_hunt_delete_says_review_required_while_the_runner_still_has_it(
        self, client, admin_headers, deploy_repo
    ):
        _rebind(deploy_repo, environment="dev", mode="solo")
        assert (
            client.post(
                "/api/v1/hunts", json={"name": "windows_hunt", **_HUNT}, headers=admin_headers
            ).status_code
            == 201
        )

        forge = _RecordingForge(url="http://forge/pr/43")
        _rebind(deploy_repo, environment="production", mode="team", forge=forge)
        resp = client.delete("/api/v1/hunts/windows_hunt", headers=admin_headers)

        assert resp.status_code == 204
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/43"
        assert (deploy_repo.repo_path / "config" / "hunts" / "windows_hunt.yaml").is_file()

    def test_a_rule_create_says_review_required(self, client, admin_headers, deploy_repo):
        forge = _RecordingForge(url="http://forge/pr/44")
        _rebind(deploy_repo, environment="production", mode="team", forge=forge)

        resp = client.post("/api/v1/rules", json=_rule_payload(), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/44"
        assert not (deploy_repo.repo_path / "config" / "rules" / "certutil.yaml").exists()

    def test_a_rule_delete_says_review_required(self, client, admin_headers, deploy_repo):
        _rebind(deploy_repo, environment="dev", mode="solo")
        assert (
            client.post("/api/v1/rules", json=_rule_payload(), headers=admin_headers).status_code
            == 201
        )

        forge = _RecordingForge(url="http://forge/pr/45")
        _rebind(deploy_repo, environment="production", mode="team", forge=forge)
        resp = client.delete("/api/v1/rules/certutil", headers=admin_headers)

        assert resp.status_code == 204
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert (deploy_repo.repo_path / "config" / "rules" / "certutil.yaml").is_file()

    def test_no_forge_still_reports_review_rather_than_committing_to_main(
        self, client, admin_headers, deploy_repo
    ):
        """No forge means no PR to name, but the change is still off main."""
        _rebind(deploy_repo, environment="production", mode="team")
        main_before = deploy_repo.head_revision()

        resp = client.post(
            "/api/v1/hunts", json={"name": "windows_hunt", **_HUNT}, headers=admin_headers
        )

        assert resp.status_code == 201, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert "X-DFE-PR-Url" not in resp.headers
        assert deploy_repo.head_revision() == main_before
