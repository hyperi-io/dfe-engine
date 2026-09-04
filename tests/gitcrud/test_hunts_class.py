#  Project:      dfe-engine
#  File:         tests/gitcrud/test_hunts_class.py
#  Purpose:      Hunts and rules reach the deploy repo the hunt runner git-syncs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""HuntConfigRegistry / RuleRegistry over the gitcrud `hunts` and `rules` classes.

Real dulwich repos throughout, no mocks. The k8s hunt runner reads its hunt and
rule YAML off a git-sync of the deploy repo's ``config/hunts`` and ``config/rules``
(dfe-infra#213), so what is asserted is the artefact: the file is at the path the
sidecar syncs, it is byte-for-byte what the directory backend writes, and the
runner's own loader and rule compiler read it back.

The forge is a recording double at the ForgeProvider seam, matching
test_routing.py; its REST behaviour is covered in test_forge.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dulwich.repo import Repo

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunt_runner.spec_loader import load_specs
from dfe_engine.hunts.deploy_repo import DeployRepoStore
from dfe_engine.hunts.hunt_config_registry import HuntConfigNotFoundError, HuntConfigRegistry
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleNotFoundError, RuleRegistry

HUNT_CONFIG = {
    "display_name": "Certutil Abuse",
    "schedule": {"mode": "rate", "interval": "5m"},
    "rules": [{"rule_name": "certutil"}],
    "global_source_table_name": "dfe.default",
    "global_target_table_name": "dfe.detection",
}

RULE = Rule(
    rule_id="certutil",
    name="Certutil Abuse",
    severity="critical",
    where_clause="process_name = 'certutil.exe'",
)


class _RecordingForge:
    """A ForgeProvider seam double that records the PR it was asked to open."""

    def __init__(self, url: str = "http://forge/pr/1") -> None:
        self.calls: list[dict] = []
        self._url = url

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequest(number=1, url=self._url, branch=head)


@pytest.fixture
def crud(tmp_path):
    """GitCrud over a fresh local (no-remote) deploy repo, default registry."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo)


def _store(crud, cls_name, *, environment="dev", mode="team", forge=None) -> DeployRepoStore:
    return DeployRepoStore(crud, cls_name, environment=environment, mode=mode, forge=forge)


@pytest.fixture
def hunts(crud):
    return HuntConfigRegistry(deploy_repo=_store(crud, "hunts"))


@pytest.fixture
def rules(crud):
    return RuleRegistry(deploy_repo=_store(crud, "rules"))


def _head_message(crud: GitCrud) -> str:
    with Repo(str(crud.repo_path)) as repo:
        return repo[repo.head()].message.decode()


class TestHuntsLandWhereTheRunnerLooks:
    def test_a_saved_hunt_is_a_file_under_config_hunts(self, hunts, crud):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        assert (crud.repo_path / "config" / "hunts" / "windows_hunt.yaml").is_file()

    def test_a_saved_rule_is_a_file_under_config_rules(self, rules, crud):
        rules.save(RULE)
        assert (crud.repo_path / "config" / "rules" / "certutil.yaml").is_file()

    def test_each_save_is_one_commit(self, hunts, crud):
        before = crud.head_revision()
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        mid = crud.head_revision()
        assert mid != before
        hunts.save("linux_hunt", dict(HUNT_CONFIG))
        assert crud.head_revision() != mid

    def test_the_runner_loads_the_synced_hunt_and_compiles_its_rule(self, hunts, rules, crud):
        """The whole point of dfe-infra#212: what git-sync delivers has to parse.

        The paths here are the two the sidecar mounts, so this is the runner's own
        read of the engine's own write - no fixture stands in between.
        """
        rules.save(RULE)
        hunts.save("windows_hunt", dict(HUNT_CONFIG))

        hunts_dir = crud.repo_path / "config" / "hunts"
        rules_dir = crud.repo_path / "config" / "rules"

        specs = load_specs(hunts_dir, rules_dir=rules_dir)
        assert list(specs) == ["windows_hunt"]
        spec = specs["windows_hunt"]
        assert spec.interval_seconds == 300
        assert spec.target_table == "dfe.detection"
        assert len(spec.queries) == 1
        assert "WHERE {window} AND (process_name = 'certutil.exe')" in spec.queries[0]

    def test_a_rule_compiles_off_the_synced_rules_directory(self, rules, crud):
        rules.save(RULE)
        sql = compile_hunt_queries(
            dict(HUNT_CONFIG), "windows_hunt", rules_dir=crud.repo_path / "config" / "rules"
        )
        assert len(sql) == 1
        assert sql[0].startswith("INSERT INTO dfe.detection")


class TestTheBytesAreUnchanged:
    """The runner parses these files unchanged, so the two backends must agree."""

    def test_hunt_yaml_matches_the_directory_backend_byte_for_byte(self, hunts, crud, tmp_path):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        local = HuntConfigRegistry(
            hunts_directory=tmp_path / "local-hunts", writable=True, refresh_interval=0
        )
        try:
            local.save("windows_hunt", dict(HUNT_CONFIG))
        finally:
            local.close()
        assert (crud.repo_path / "config" / "hunts" / "windows_hunt.yaml").read_bytes() == (
            tmp_path / "local-hunts" / "windows_hunt.yaml"
        ).read_bytes()

    def test_rule_yaml_matches_the_directory_backend_byte_for_byte(self, rules, crud, tmp_path):
        rules.save(RULE)
        local = RuleRegistry(
            rules_directory=tmp_path / "local-rules", writable=True, refresh_interval=0
        )
        try:
            local.save(RULE)
        finally:
            local.close()
        assert (crud.repo_path / "config" / "rules" / "certutil.yaml").read_bytes() == (
            tmp_path / "local-rules" / "certutil.yaml"
        ).read_bytes()

    def test_no_metadata_block_rides_on_the_stored_doc(self, hunts):
        """Unlike sources: an extra key here is a key the runner's loader parses."""
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        assert set(hunts.get("windows_hunt")) == set(HUNT_CONFIG)


class TestReadBack:
    def test_get_and_exists_read_the_deploy_repo(self, hunts):
        assert hunts.exists("windows_hunt") is False
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        assert hunts.exists("windows_hunt") is True
        assert hunts.get("windows_hunt")["global_target_table_name"] == "dfe.detection"

    def test_name_exists_is_case_insensitive(self, hunts):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        assert hunts.name_exists("Windows_Hunt") is True

    def test_list_hunts_summarises_the_stored_docs(self, hunts):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        rows = hunts.list_hunts()
        assert len(rows) == 1
        assert rows[0]["name"] == "windows_hunt"
        assert rows[0]["display_name"] == "Certutil Abuse"
        assert rows[0]["rules"] == ["certutil"]

    def test_hunt_names_referencing_rule(self, hunts):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        assert hunts.hunt_names_referencing_rule("certutil") == ["windows_hunt"]

    def test_list_rules_summarises_the_stored_docs(self, rules):
        rules.save(RULE)
        rows = rules.list_rules()
        assert [row["name"] for row in rows] == ["certutil"]
        assert rows[0]["display_name"] == "Certutil Abuse"

    def test_get_rule_round_trips(self, rules):
        rules.save(RULE)
        loaded = rules.get("certutil")
        assert loaded.name == "Certutil Abuse"
        assert loaded.where_clause == "process_name = 'certutil.exe'"


class TestDelete:
    def test_delete_removes_the_file_and_commits_it(self, hunts, crud):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        head = crud.head_revision()
        hunts.delete("windows_hunt", created_by="kaz")
        assert not (crud.repo_path / "config" / "hunts" / "windows_hunt.yaml").exists()
        assert crud.head_revision() != head
        assert hunts.exists("windows_hunt") is False

    def test_deleting_a_rule_removes_it_from_the_synced_directory(self, rules, crud):
        rules.save(RULE)
        rules.delete("certutil", created_by="kaz")
        assert not (crud.repo_path / "config" / "rules" / "certutil.yaml").exists()

    def test_deleting_a_missing_hunt_raises(self, hunts):
        with pytest.raises(HuntConfigNotFoundError):
            hunts.delete("nope")

    def test_deleting_a_missing_rule_raises(self, rules):
        with pytest.raises(RuleNotFoundError):
            rules.delete("nope")

    def test_a_traversing_name_reads_as_absent_not_as_a_500(self, hunts):
        """`{name}` is a path param nothing validates, so it reaches the store raw."""
        assert hunts.exists("..") is False
        with pytest.raises(HuntConfigNotFoundError):
            hunts.get("..")
        with pytest.raises(HuntConfigNotFoundError):
            hunts.delete("..")


class TestCommitMessages:
    def test_a_create_commits_a_conforming_subject_and_actor_trailer(self, hunts, crud):
        hunts.save("windows_hunt", dict(HUNT_CONFIG), created_by="kaz")
        message = _head_message(crud)
        assert message.startswith("hunt(windows_hunt): create")
        assert "DFE-Actor: kaz" in message

    def test_a_second_save_commits_as_an_update(self, hunts, crud):
        hunts.save("windows_hunt", dict(HUNT_CONFIG), created_by="kaz")
        hunts.save("windows_hunt", {**HUNT_CONFIG, "customers": ["acme"]}, created_by="kaz")
        assert _head_message(crud).startswith("hunt(windows_hunt): update")

    def test_a_delete_commits_as_a_delete(self, hunts, crud):
        hunts.save("windows_hunt", dict(HUNT_CONFIG))
        hunts.delete("windows_hunt", created_by="kaz")
        assert _head_message(crud).startswith("hunt(windows_hunt): delete")

    def test_a_rule_commits_under_the_same_type(self, rules, crud):
        rules.save(RULE, created_by="kaz")
        assert _head_message(crud).startswith("hunt(certutil): create")


class TestWritePosture:
    """auto-merge / PR posture, the same gate every other governed class takes."""

    def test_dev_posture_commits_straight_to_main(self, crud):
        registry = HuntConfigRegistry(deploy_repo=_store(crud, "hunts", environment="dev"))
        outcome = registry.save("windows_hunt", dict(HUNT_CONFIG))
        assert registry.exists("windows_hunt") is True
        assert outcome is not None
        assert outcome.review_required is False

    def test_production_team_routes_to_a_review_pr_and_leaves_main_alone(self, crud):
        # An empty repo has no base to branch from, so main carries a first commit.
        crud.put("hunts", "seed", dict(HUNT_CONFIG), "seed")
        forge = _RecordingForge(url="http://forge/pr/42")
        registry = HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment="production", mode="team", forge=forge)
        )

        outcome = registry.save("windows_hunt", dict(HUNT_CONFIG), created_by="kaz")

        assert len(forge.calls) == 1
        assert forge.calls[0]["base"] == "main"
        assert forge.calls[0]["head"].startswith("dfe/hunt/hunts-windows_hunt/")
        # main never got the file, so the git-sync sidecar does not serve it yet
        assert not (crud.repo_path / "config" / "hunts" / "windows_hunt.yaml").exists()
        # ... and the caller is told, so it cannot report the hunt as created
        assert outcome is not None
        assert outcome.review_required is True
        assert outcome.pr_url == "http://forge/pr/42"

    def test_production_team_without_a_forge_commits_to_a_branch_not_main(self, crud):
        crud.put("hunts", "seed", dict(HUNT_CONFIG), "seed")
        main_before = crud.head_revision()
        registry = HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment="production", mode="team")
        )

        outcome = registry.save("windows_hunt", dict(HUNT_CONFIG), created_by="kaz")

        assert crud.head_revision() == main_before
        assert not (crud.repo_path / "config" / "hunts" / "windows_hunt.yaml").exists()
        with Repo(str(crud.repo_path)) as repo:
            branches = [k.decode() for k in repo.refs.allkeys() if b"dfe/hunt/" in k]
        assert len(branches) == 1
        assert branches[0].startswith("refs/heads/dfe/hunt/hunts-windows_hunt/")
        assert outcome is not None
        assert outcome.review_required is True
        assert branches[0] == f"refs/heads/{outcome.branch}"

    def test_a_production_team_hunt_delete_reports_the_review_branch(self, crud):
        registry = HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment="dev", mode="solo")
        )
        registry.save("windows_hunt", dict(HUNT_CONFIG))

        forge = _RecordingForge(url="http://forge/pr/7")
        reviewed = HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment="production", mode="team", forge=forge)
        )
        outcome = reviewed.delete("windows_hunt", created_by="kaz")

        # The runner keeps executing the hunt until the branch is merged.
        assert (crud.repo_path / "config" / "hunts" / "windows_hunt.yaml").is_file()
        assert outcome is not None
        assert outcome.review_required is True
        assert outcome.pr_url == "http://forge/pr/7"

    def test_a_production_team_rule_write_and_delete_report_review(self, crud):
        direct = RuleRegistry(deploy_repo=_store(crud, "rules", environment="dev", mode="solo"))
        direct.save(RULE)

        forge = _RecordingForge(url="http://forge/pr/8")
        reviewed = RuleRegistry(
            deploy_repo=_store(crud, "rules", environment="production", mode="team", forge=forge)
        )
        # A no-op write opens no PR, so the edit has to actually change the rule.
        saved = reviewed.save(RULE.model_copy(update={"severity": "high"}), created_by="kaz")
        deleted = reviewed.delete("certutil", created_by="kaz")

        assert saved is not None
        assert saved.review_required is True
        assert deleted is not None
        assert deleted.review_required is True
        assert (crud.repo_path / "config" / "rules" / "certutil.yaml").is_file()

    def test_the_directory_backend_reports_no_routing_outcome(self, tmp_path: Path):
        registry = HuntConfigRegistry(
            hunts_directory=tmp_path / "hunts", writable=True, refresh_interval=0
        )
        try:
            assert registry.save("windows_hunt", dict(HUNT_CONFIG)) is None
            assert registry.delete("windows_hunt") is None
        finally:
            registry.close()

    def test_solo_posture_commits_direct_in_production(self, crud):
        registry = HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment="production", mode="solo")
        )
        registry.save("windows_hunt", dict(HUNT_CONFIG))
        assert registry.exists("windows_hunt") is True

    def test_the_stored_auto_merge_flag_is_the_dial(self, crud):
        """governance/settings/gitops.yaml, read through the same resolve_state."""
        from dfe_engine.gitcrud.auto_merge import resolve_state, set_stored

        set_stored(crud, True, "kaz")
        state = resolve_state(crud, environment="production", mode="solo")
        assert state.stored is True
        assert state.effective is True

        registry = HuntConfigRegistry(
            deploy_repo=_store(crud, "hunts", environment="production", mode="solo")
        )
        registry.save("windows_hunt", dict(HUNT_CONFIG))
        assert registry.exists("windows_hunt") is True


class TestDirectoryBackendUnchanged:
    """The docker tier: no deploy repo, so the shared config volume is the path."""

    def test_a_hunt_still_lands_in_the_configured_directory(self, tmp_path: Path):
        registry = HuntConfigRegistry(
            hunts_directory=tmp_path / "hunts", writable=True, refresh_interval=0
        )
        try:
            registry.save("windows_hunt", dict(HUNT_CONFIG))
            assert (tmp_path / "hunts" / "windows_hunt.yaml").is_file()
            assert registry.get("windows_hunt")["display_name"] == "Certutil Abuse"
            registry.delete("windows_hunt")
            assert not (tmp_path / "hunts" / "windows_hunt.yaml").exists()
        finally:
            registry.close()

    def test_a_rule_still_lands_in_the_configured_directory(self, tmp_path: Path):
        registry = RuleRegistry(
            rules_directory=tmp_path / "rules", writable=True, refresh_interval=0
        )
        try:
            registry.save(RULE)
            assert (tmp_path / "rules" / "certutil.yaml").is_file()
            assert registry.get("certutil").severity == "critical"
            registry.delete("certutil")
            assert not (tmp_path / "rules" / "certutil.yaml").exists()
        finally:
            registry.close()

    def test_a_registry_with_neither_backend_refuses_to_build(self, tmp_path: Path):
        with pytest.raises(Exception, match="hunts_directory is required"):
            HuntConfigRegistry()
        with pytest.raises(Exception, match="rules_directory is required"):
            RuleRegistry()
