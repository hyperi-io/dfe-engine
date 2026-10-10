#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_repo_failed_write.py
#  Purpose:      A deploy-repo write that fails leaves nothing staged for the next one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A write that raises part way does not hand its change to the next write.

Every write stages into the clone's one index, so whatever a failed write left there
would go into the next commit under that write's message and author, and out with its
push. A file it wrote but never staged would stay in the tree, where a reader of the
working tree loads it as committed config. Real git, tempdirs, no mocks: a pre-commit
hook refuses the commit, and an object directory that points nowhere refuses a blob.
"""

import stat
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.errors import CommitError
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit
from dulwich.refs import Ref
from dulwich.repo import Repo

from dfe_engine.gitops.repo import GitopsRepo
from tests.support.deploy_remote import seeded_remote

# The file a write gets as far as writing, but whose blob the object store refuses.
_UNSTAGED = b"b: unstaged\n"


def _refuse_commits(repo: GitopsRepo) -> Path:
    """Install a pre-commit hook that fails every commit; returns it for removal."""
    hook = repo.path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    return hook


def _refuse_blob(repo: GitopsRepo, tmp_path: Path, content: bytes) -> Path:
    """Point the blob's object directory nowhere, which refuses it for root as well."""
    fanout = repo.path / ".git" / "objects" / Blob.from_string(content).id.decode()[:2]
    assert not fanout.exists()
    fanout.symlink_to(tmp_path / "gone")
    return fanout


def _head_commit(repo_path: Path, ref: bytes = b"HEAD") -> tuple[bytes, dict[str, bytes]]:
    """The head commit's message, and every file in its tree with its content."""
    with Repo(str(repo_path)) as git:
        commit = git[git.refs[Ref(ref)]]
        assert isinstance(commit, Commit)
        files: dict[str, bytes] = {}
        for entry in iter_tree_contents(git.object_store, commit.tree):
            blob = git[entry.sha]
            assert isinstance(blob, Blob)
            files[entry.path.decode()] = blob.data
        return commit.message, files


def _assert_tree_is_head(repo: GitopsRepo) -> None:
    """The index and the working tree match HEAD exactly, with no untracked file."""
    status = porcelain.status(str(repo.path))
    assert status.staged == {"add": [], "delete": [], "modify": []}
    assert status.unstaged == []
    assert status.untracked == []


def _local_clone(tmp_path: Path) -> GitopsRepo:
    repo = GitopsRepo(local_path=str(tmp_path / "work"), push=False)
    repo.ensure()
    repo.publish({"seed.yaml": "seed: 1\n"}, message="seed")
    return repo


def _assert_next_write_lands_alone(repo: GitopsRepo) -> None:
    repo.publish({"next.yaml": "next: 1\n"}, message="write B")
    message, files = _head_commit(repo.path)
    assert message == b"write B"
    assert files == {"seed.yaml": b"seed: 1\n", "next.yaml": b"next: 1\n"}


def test_a_refused_commit_leaves_nothing_for_the_next_write(tmp_path: Path) -> None:
    repo = _local_clone(tmp_path)
    hook = _refuse_commits(repo)

    with pytest.raises(CommitError):
        repo.publish({"a.yaml": "a: 1\n", "seed.yaml": "seed: 2\n"}, message="write A")
    hook.unlink()

    _assert_tree_is_head(repo)
    assert not (repo.path / "a.yaml").exists()
    assert (repo.path / "seed.yaml").read_text() == "seed: 1\n"
    _assert_next_write_lands_alone(repo)


def test_a_refused_commit_is_not_pushed_by_the_next_write(tmp_path: Path) -> None:
    remote, branch = seeded_remote(tmp_path)
    repo = GitopsRepo(
        local_path=str(tmp_path / "work"), repo_url=remote.as_uri(), branch=branch, push=True
    )
    repo.ensure()
    hook = _refuse_commits(repo)

    with pytest.raises(CommitError):
        repo.publish({"a.yaml": "a: 1\n"}, message="write A")
    hook.unlink()

    _assert_tree_is_head(repo)
    assert repo.publish({"b.yaml": "b: 1\n"}, message="write B").pushed
    message, files = _head_commit(remote, f"refs/heads/{branch}".encode())
    assert message == b"write B"
    assert files == {"README": b"seed\n", "b.yaml": b"b: 1\n"}


def test_a_batch_whose_commit_is_refused_leaves_nothing_for_the_next_write(
    tmp_path: Path,
) -> None:
    repo = _local_clone(tmp_path)
    hook = _refuse_commits(repo)

    with pytest.raises(CommitError), repo.batch("batch A"):
        repo.publish({"a.yaml": "a: 1\n"}, message="write A")
    hook.unlink()

    _assert_tree_is_head(repo)
    _assert_next_write_lands_alone(repo)


def test_a_write_that_fails_part_way_through_staging_leaves_nothing_staged(
    tmp_path: Path,
) -> None:
    repo = _local_clone(tmp_path)

    # x.yaml is written and staged first, then x.yaml/y.yaml cannot have a directory.
    with pytest.raises(FileExistsError):
        repo.publish({"x.yaml": "x: 1\n", "x.yaml/y.yaml": "y: 1\n"}, message="write A")

    _assert_tree_is_head(repo)
    _assert_next_write_lands_alone(repo)


def test_a_file_written_but_never_staged_is_not_left_in_the_tree(tmp_path: Path) -> None:
    repo = _local_clone(tmp_path)
    refused = _refuse_blob(repo, tmp_path, _UNSTAGED)

    try:
        with pytest.raises(OSError):
            repo.publish({"a.yaml": "a: 1\n", "b.yaml": _UNSTAGED.decode()}, message="write A")
    finally:
        refused.unlink()

    _assert_tree_is_head(repo)
    assert not (repo.path / "a.yaml").exists()
    assert not (repo.path / "b.yaml").exists()
    _assert_next_write_lands_alone(repo)


def test_a_batch_write_that_fails_part_way_leaves_nothing_in_the_tree(tmp_path: Path) -> None:
    repo = _local_clone(tmp_path)
    refused = _refuse_blob(repo, tmp_path, _UNSTAGED)

    try:
        with pytest.raises(OSError), repo.batch("batch A"):
            repo.publish({"a.yaml": "a: 1\n"}, message="write A")
            repo.publish({"b.yaml": _UNSTAGED.decode()}, message="write B")
    finally:
        refused.unlink()

    _assert_tree_is_head(repo)
    assert not (repo.path / "a.yaml").exists()
    assert not (repo.path / "b.yaml").exists()
    _assert_next_write_lands_alone(repo)
