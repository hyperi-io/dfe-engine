#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_review_branch_files.py
#  Purpose:      What review branches not yet merged would change in a deploy-repo directory
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``GitopsRepo.review_branch_files`` reads each review branch against its merge base.

A pushing clone reads the remote's branches, as every replica's reviews live there,
and any other clone reads its own.
"""

from pathlib import Path

from dulwich import porcelain
from support.deploy_remote import seeded_remote

from dfe_engine.gitops.repo import GitopsRepo, ReviewBranchFile


def _local(tmp_path: Path) -> GitopsRepo:
    repo = GitopsRepo(local_path=str(tmp_path / "work"), push=False)
    repo.ensure()
    repo.publish({"config/hunts/a.yaml": "a: 1\n", "config/hunts/b.yaml": "b: 1\n"}, "seed")
    return repo


def _pushing(tmp_path: Path, name: str, remote: Path, branch: str) -> GitopsRepo:
    repo = GitopsRepo(
        local_path=str(tmp_path / name), repo_url=str(remote), branch=branch, push=True
    )
    repo.ensure()
    return repo


def test_a_review_branch_lists_the_files_it_changes_beside_their_base(tmp_path):
    repo = _local(tmp_path)
    repo.publish(
        {"config/hunts/a.yaml": "a: 2\n", "config/hunts/c.yaml": "c: 1\n"},
        "r",
        branch="dfe/hunt/x/1",
    )

    assert repo.review_branch_files("config/hunts") == [
        ReviewBranchFile(
            branch="dfe/hunt/x/1", name="a.yaml", content="a: 2\n", base_content="a: 1\n"
        ),
        ReviewBranchFile(branch="dfe/hunt/x/1", name="c.yaml", content="c: 1\n", base_content=None),
    ]


def test_a_change_main_took_after_the_branch_is_not_the_branchs(tmp_path):
    repo = _local(tmp_path)
    repo.publish({"config/hunts/a.yaml": "a: 2\n"}, "r", branch="dfe/hunt/x/1")
    repo.publish({"config/hunts/b.yaml": "b: 2\n"}, "main moves on")

    assert [f.name for f in repo.review_branch_files("config/hunts")] == ["a.yaml"]


def test_a_branch_main_already_holds_is_merged(tmp_path):
    repo = _local(tmp_path)
    repo.publish({"config/hunts/a.yaml": "a: 2\n"}, "r", branch="dfe/hunt/x/1")
    porcelain.merge(str(repo.path), b"dfe/hunt/x/1", author=b"t <t@t>", committer=b"t <t@t>")

    assert repo.review_branch_files("config/hunts") == []


def test_a_branch_outside_the_review_prefix_is_not_a_review(tmp_path):
    repo = _local(tmp_path)
    repo.publish({"config/hunts/a.yaml": "a: 2\n"}, "r", branch="feature/x")

    assert repo.review_branch_files("config/hunts") == []


def test_a_pushing_clone_reads_a_review_another_replica_pushed(tmp_path):
    remote, branch = seeded_remote(tmp_path)
    writer = _pushing(tmp_path, "a", remote, branch)
    reader = _pushing(tmp_path, "b", remote, branch)
    writer.publish({"config/hunts/a.yaml": "a: 2\n"}, "r", branch="dfe/hunt/x/1")

    assert [(f.branch, f.name) for f in reader.review_branch_files("config/hunts")] == [
        ("dfe/hunt/x/1", "a.yaml")
    ]


def test_deleting_a_review_on_the_remote_releases_it_from_every_clone(tmp_path):
    remote, branch = seeded_remote(tmp_path)
    writer = _pushing(tmp_path, "a", remote, branch)
    writer.publish({"config/hunts/a.yaml": "a: 2\n"}, "r", branch="dfe/hunt/x/1")

    porcelain.push(str(writer.path), str(remote), b":refs/heads/dfe/hunt/x/1")

    assert writer.review_branch_files("config/hunts") == []
