"""Real-git tests for GitopsRepo (dulwich, tempdirs -- no mocks)."""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest
from dulwich import porcelain

from dfe_engine.gitops.repo import GitopsRemoteError, GitopsRepo, PublishResult


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    return str(remote)


def test_publish_writes_commits_and_reports_change(tmp_path: Path) -> None:
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url="", branch="main", push=False)
    repo.ensure()
    result = repo.publish(
        {
            "argocd/appproject-local.yaml": "kind: AppProject\n",
            "ddl/x.sql": "CREATE TABLE x;\n",
        },
        message="seed",
    )
    assert isinstance(result, PublishResult)
    assert result.changed is True
    assert result.commit_sha
    assert "argocd/appproject-local.yaml" in result.files
    assert (work / "ddl" / "x.sql").read_text() == "CREATE TABLE x;\n"


def test_publish_is_noop_when_unchanged(tmp_path: Path) -> None:
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url="", branch="main", push=False)
    repo.ensure()
    arts = {"a.yaml": "x: 1\n"}
    first = repo.publish(arts, message="one")
    assert first.changed is True
    second = repo.publish(arts, message="two")
    assert second.changed is False
    assert second.commit_sha is None


def test_publish_clones_and_pushes_to_remote(tmp_path: Path) -> None:
    remote = _bare_remote(tmp_path)
    work = tmp_path / "work"
    # Seed the bare remote with an initial commit so it has a default branch.
    seed = tmp_path / "seed"
    porcelain.clone(remote, str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), remote, f"refs/heads/{branch}".encode())

    repo = GitopsRepo(local_path=str(work), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    res = repo.publish({"f.yaml": "v: 1\n"}, message="push me")
    assert res.changed is True
    assert res.pushed is True

    check = tmp_path / "check"
    porcelain.clone(remote, str(check))
    assert (check / "f.yaml").read_text() == "v: 1\n"


def _seeded_remote(tmp_path: Path) -> tuple[str, str]:
    """A bare remote with one commit, and the branch it lives on."""
    remote = _bare_remote(tmp_path)
    seed = tmp_path / "seed"
    porcelain.clone(remote, str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), remote, f"refs/heads/{branch}".encode())
    return remote, branch


def _remote_files(tmp_path: Path, remote: str, name: str) -> set[str]:
    check = tmp_path / name
    porcelain.clone(remote, str(check))
    return {p.name for p in check.iterdir() if p.is_file() and p.name != "README"}


def test_a_second_clone_publishes_on_the_remote_head(tmp_path: Path) -> None:
    """Two replicas, two clones: the write lands on top of what the other pushed."""
    remote, branch = _seeded_remote(tmp_path)
    first = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    second = GitopsRepo(local_path=str(tmp_path / "b"), repo_url=remote, branch=branch, push=True)
    first.ensure()
    second.ensure()

    assert first.publish({"a.yaml": "a: 1\n"}, message="from a").pushed is True
    res = second.publish({"b.yaml": "b: 1\n"}, message="from b")
    assert res.pushed is True

    assert _remote_files(tmp_path, remote, "check") == {"a.yaml", "b.yaml"}
    assert (tmp_path / "b" / "a.yaml").read_text() == "a: 1\n"


def test_a_push_the_remote_moved_under_is_re_applied_once(tmp_path: Path, monkeypatch) -> None:
    """The remote moves between the sync and the push: the write is redone on its head."""
    remote, branch = _seeded_remote(tmp_path)
    first = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    second = GitopsRepo(local_path=str(tmp_path / "b"), repo_url=remote, branch=branch, push=True)
    first.ensure()
    second.ensure()

    real_sync = second.sync
    calls: list[bool] = []

    def racing_sync(*, discard_local: bool = False) -> bool:
        # The first sync sees a quiet remote; the other replica then pushes before
        # this clone's push goes out.
        calls.append(discard_local)
        if len(calls) == 1:
            moved = real_sync(discard_local=discard_local)
            first.publish({"a.yaml": "a: 1\n"}, message="from a")
            return moved
        return real_sync(discard_local=discard_local)

    monkeypatch.setattr(second, "sync", racing_sync)
    res = second.publish({"b.yaml": "b: 1\n"}, message="from b")
    assert res.pushed is True
    assert calls == [False, True]

    assert _remote_files(tmp_path, remote, "check") == {"a.yaml", "b.yaml"}


def test_a_rejected_push_with_a_quiet_remote_stays_an_error(tmp_path: Path, monkeypatch) -> None:
    """Only a moved remote earns the retry; any other rejection is raised as it was."""
    remote, branch = _seeded_remote(tmp_path)
    repo = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    repo.ensure()

    def refuse(refspec: bytes) -> None:
        raise GitopsRemoteError("remote said no")

    monkeypatch.setattr(repo, "_push_refspec", refuse)
    try:
        repo.publish({"a.yaml": "a: 1\n"}, message="from a")
    except GitopsRemoteError as exc:
        assert "remote said no" in str(exc)
    else:
        raise AssertionError("a rejected push on a quiet remote must raise")


def _local_commit(work: Path, rel: str, content: str, message: bytes) -> None:
    """Commit a file in ``work`` without pushing it -- a replica mid-write."""
    target = work / rel
    target.write_text(content, encoding="utf-8")
    porcelain.add(str(work), paths=[str(target)])
    porcelain.commit(str(work), message=message, author=b"t <t@t>", committer=b"t <t@t>")


def test_two_clones_writing_on_the_same_base_both_land(tmp_path: Path) -> None:
    """Two replicas, one deploy repo: neither write is lost and neither clone strands."""
    remote, branch = _seeded_remote(tmp_path)
    first = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    second = GitopsRepo(local_path=str(tmp_path / "b"), repo_url=remote, branch=branch, push=True)
    first.ensure()
    second.ensure()

    assert first.publish({"a.yaml": "a: 1\n"}, message="from a").pushed is True
    assert second.publish({"b.yaml": "b: 1\n"}, message="from b").pushed is True
    assert _remote_files(tmp_path, remote, "check") == {"a.yaml", "b.yaml"}

    assert first.publish({"c.yaml": "c: 1\n"}, message="from a again").pushed is True
    assert second.publish({"d.yaml": "d: 1\n"}, message="from b again").pushed is True
    assert _remote_files(tmp_path, remote, "check2") == {"a.yaml", "b.yaml", "c.yaml", "d.yaml"}


def test_a_stranded_clone_still_serves_its_next_write(tmp_path: Path) -> None:
    """A clone left holding a commit the remote never took keeps writing, on its head."""
    remote, branch = _seeded_remote(tmp_path)
    first = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    second = GitopsRepo(local_path=str(tmp_path / "b"), repo_url=remote, branch=branch, push=True)
    first.ensure()
    second.ensure()

    # The state a lost push race leaves behind: a commit on the old base here, and a
    # remote that has moved past it from the other replica.
    _local_commit(tmp_path / "b", "stranded.yaml", "s: 1\n", b"stranded")
    first.publish({"a.yaml": "a: 1\n"}, message="from a")

    res = second.publish({"b.yaml": "b: 1\n"}, message="from b")

    assert res.pushed is True
    assert _remote_files(tmp_path, remote, "check") == {"a.yaml", "b.yaml"}


def test_refresh_takes_a_commit_the_other_clone_pushed(tmp_path: Path) -> None:
    """A clone only sees another replica's write once it takes the remote's head."""
    remote, branch = _seeded_remote(tmp_path)
    first = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    second = GitopsRepo(local_path=str(tmp_path / "b"), repo_url=remote, branch=branch, push=True)
    first.ensure()
    second.ensure()

    first.publish({"a.yaml": "a: 1\n"}, message="from a")
    assert not (tmp_path / "b" / "a.yaml").exists()

    assert second.refresh() is True
    assert (tmp_path / "b" / "a.yaml").read_text() == "a: 1\n"
    assert second.head_revision() == first.head_revision()


def test_refresh_is_a_noop_when_the_remote_has_not_moved(tmp_path: Path) -> None:
    remote, branch = _seeded_remote(tmp_path)
    repo = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    head = repo.head_revision()

    assert repo.refresh() is False
    assert repo.head_revision() == head


def test_refresh_leaves_a_clone_that_does_not_push_alone(tmp_path: Path) -> None:
    """A clone holding a commit it never pushed is the only copy: never reset it."""
    remote, branch = _seeded_remote(tmp_path)
    pusher = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    keeper = GitopsRepo(local_path=str(tmp_path / "b"), repo_url=remote, branch=branch, push=False)
    pusher.ensure()
    keeper.ensure()

    _local_commit(tmp_path / "b", "local.yaml", "l: 1\n", b"local only")
    pusher.publish({"a.yaml": "a: 1\n"}, message="from a")

    assert keeper.refresh() is False
    assert (tmp_path / "b" / "local.yaml").read_text() == "l: 1\n"


def test_refresh_serves_this_clone_when_the_deploy_repo_is_unreachable(tmp_path: Path) -> None:
    """A forge blip must not fail a read: the clone keeps answering from what it has."""
    remote, branch = _seeded_remote(tmp_path)
    repo = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    head = repo.head_revision()
    shutil.rmtree(remote)

    assert repo.refresh() is False
    assert repo.head_revision() == head
    assert (tmp_path / "a" / "README").read_text() == "seed\n"


def test_a_refused_ref_raises_instead_of_reporting_a_successful_push(tmp_path: Path) -> None:
    """dulwich reports a refused ref in ref_status and returns normally; we must not."""
    remote, branch = _seeded_remote(tmp_path)
    repo = GitopsRepo(local_path=str(tmp_path / "a"), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    _local_commit(tmp_path / "a", "a.yaml", "a: 1\n", b"from a")

    # HEAD resolves to the branch, so the second update is stale by the time it is
    # applied -- the refusal a ref another replica moved produces, on demand.
    branch_ref = f"refs/heads/{branch}".encode()
    result = porcelain.push(
        str(tmp_path / "a"),
        remote,
        [branch_ref, branch_ref + b":HEAD"],
        errstream=io.BytesIO(),
    )
    assert result.ref_status[b"HEAD"] is not None

    with pytest.raises(GitopsRemoteError, match="refused"):
        repo._raise_on_refused(result)
