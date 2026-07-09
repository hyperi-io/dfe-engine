"""Real-git tests for GitopsRepo (dulwich, tempdirs -- no mocks)."""

from __future__ import annotations

from pathlib import Path

from dulwich import porcelain

from dfe_engine.gitops.repo import GitopsRepo, PublishResult


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
