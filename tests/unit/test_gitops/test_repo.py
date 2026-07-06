"""Real-git tests for GitopsRepo (dulwich, tempdirs -- no mocks)."""

from __future__ import annotations

from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from dfe_engine.gitops.repo import (
    ConcurrencyConflict,
    GitopsRepo,
    PathEscapesRepoError,
    PublishResult,
    PushError,
    RepoDirtyError,
)


def _bare_remote(tmp_path: Path) -> str:
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    return str(remote)


def _seeded_remote(tmp_path: Path) -> tuple[str, str]:
    """Bare remote with one seed commit; returns (remote_url, branch)."""
    remote = _bare_remote(tmp_path)
    seed = tmp_path / "seed"
    porcelain.clone(remote, str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), remote, f"refs/heads/{branch}".encode())
    return remote, branch


def _head_tree_names(work: Path) -> set[str]:
    with Repo(str(work)) as r:
        tree = r[r[r.head()].tree]
        return {name.decode() for name in tree}


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


def test_midpublish_failure_restores_worktree_and_index(tmp_path: Path, monkeypatch) -> None:
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url="", branch="main", push=False)
    repo.ensure()
    repo.publish({"base.yaml": "x: 1\n"}, message="seed")

    real_write_text = Path.write_text

    def exploding_write_text(self, content, *args, **kwargs):
        if self.name == "zz.yaml":
            raise OSError("disk full")
        return real_write_text(self, content, *args, **kwargs)

    # crash mid-publish: a.yaml written+staged, base.yaml modified+staged,
    # then zz.yaml explodes - the touched paths must roll back to HEAD
    monkeypatch.setattr(Path, "write_text", exploding_write_text)
    with pytest.raises(OSError):
        repo.publish(
            {"a.yaml": "a: 1\n", "base.yaml": "x: 2\n", "zz.yaml": "z: 1\n"},
            message="boom",
        )
    monkeypatch.undo()

    assert not (work / "a.yaml").exists()
    assert (work / "base.yaml").read_text() == "x: 1\n"
    staged = porcelain.status(str(work)).staged
    assert not staged["add"]
    assert not staged["modify"]
    assert not staged["delete"]

    # the next publish must commit ONLY its own files, not swept leftovers
    res = repo.publish({"c.yaml": "c: 1\n"}, message="next")
    assert res.changed is True
    assert res.files == ["c.yaml"]
    names = _head_tree_names(work)
    assert "c.yaml" in names
    assert "a.yaml" not in names
    assert "zz.yaml" not in names


def test_publish_refuses_pre_staged_changes(tmp_path: Path) -> None:
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url="", branch="main", push=False)
    repo.ensure()
    repo.publish({"base.yaml": "x: 1\n"}, message="seed")

    (work / "stray.yaml").write_text("s: 1\n", encoding="utf-8")
    porcelain.add(str(work), paths=[str(work / "stray.yaml")])

    with pytest.raises(RepoDirtyError, match=r"stray\.yaml"):
        repo.publish({"mine.yaml": "m: 1\n"}, message="mine")


def test_publish_rejects_traversal_write(tmp_path: Path) -> None:
    """A traversal / absolute / NUL artifact path is refused before any write can
    escape the repo root (F-GITCRUD-TRAVERSAL)."""
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url="", branch="main", push=False)
    repo.ensure()
    outside = tmp_path / "pwned.yaml"

    with pytest.raises(PathEscapesRepoError):
        repo.publish({"../pwned.yaml": "x: 1\n"}, message="evil")
    assert not outside.exists()
    with pytest.raises(PathEscapesRepoError):
        repo.publish({str(tmp_path / "abs.yaml"): "x: 1\n"}, message="evil-abs")
    with pytest.raises(PathEscapesRepoError):
        repo.publish({"a\x00b.yaml": "x: 1\n"}, message="evil-nul")
    # a normal contained path still works
    assert repo.publish({"ok/here.yaml": "x: 1\n"}, message="fine").changed is True


def test_publish_rejects_traversal_deletion(tmp_path: Path) -> None:
    """Deletions get the same containment gate - a traversal name cannot unlink a
    file outside the repo (F-GITCRUD-TRAVERSAL)."""
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url="", branch="main", push=False)
    repo.ensure()
    victim = tmp_path / "victim.yaml"
    victim.write_text("keep: me\n", encoding="utf-8")

    with pytest.raises(PathEscapesRepoError):
        repo.publish({}, message="rm", deletions=["../victim.yaml"])
    assert victim.exists()


def test_ensure_scrubs_push_token_from_git_config(tmp_path: Path) -> None:
    """The push token must never persist in the clone's .git/config.

    porcelain.clone stores whatever URL it cloned from into remote.origin.url;
    for HTTPS token auth _authed_url() carries ``username:token@`` in plaintext,
    readable by any co-located sidecar, exec shell or volume snapshot
    (F-GITOPS-TOKEN). ensure() scrubs the stored remote back to the bare repo_url;
    push()/fetch() re-supply the credential per call. A local remote can't embed
    creds, so this reproduces the exact leaked-config state an HTTPS clone leaves,
    then proves the scrub ensure() runs removes it.
    """
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "work"
    repo = GitopsRepo(
        local_path=str(work),
        repo_url=remote,
        branch=branch,
        push=False,
        username="ci-bot",
        token="s3cr3t-push-token",
    )
    repo.ensure()

    config_path = work / ".git" / "config"
    with Repo(str(work)) as r:
        cfg = r.get_config()
        cfg.set(
            (b"remote", b"origin"),
            b"url",
            b"https://ci-bot:s3cr3t-push-token@git.example.com/deploy.git",
        )
        cfg.write_to_path()
    assert b"s3cr3t-push-token" in config_path.read_bytes()

    repo._scrub_remote_credentials()

    raw = config_path.read_bytes()
    assert b"s3cr3t-push-token" not in raw
    assert b"ci-bot" not in raw
    with Repo(str(work)) as r:
        stored = r.get_config().get((b"remote", b"origin"), b"url")
    assert stored == remote.encode()


def test_push_failure_raises_pusherror_with_local_sha(tmp_path: Path, monkeypatch) -> None:
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url=remote, branch=branch, push=True)
    repo.ensure()

    def failing_push(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(porcelain, "push", failing_push)
    with pytest.raises(PushError) as excinfo:
        repo.publish({"f.yaml": "v: 1\n"}, message="try push")
    monkeypatch.undo()

    # the local commit landed - the error must say so, not look like a
    # failed commit
    assert excinfo.value.committed_sha
    assert repo.head_revision() == excinfo.value.committed_sha


def test_push_rejection_fast_forwards_and_repushes(tmp_path: Path) -> None:
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    assert repo.publish({"f.yaml": "v: 1\n"}, message="first").pushed is True

    # the remote gains an unrelated commit our clone has never fetched
    other = tmp_path / "other"
    porcelain.clone(remote, str(other))
    (other / "other.yaml").write_text("o: 1\n", encoding="utf-8")
    porcelain.add(str(other), paths=[str(other / "other.yaml")])
    porcelain.commit(str(other), message=b"theirs", author=b"o <o@o>", committer=b"o <o@o>")
    porcelain.push(str(other), remote, f"refs/heads/{branch}".encode())

    res = repo.publish({"mine.yaml": "m: 1\n"}, message="mine")
    assert res.changed is True
    assert res.pushed is True

    # both changes survive: the reconcile fast-forwarded, never forced
    check = tmp_path / "check"
    porcelain.clone(remote, str(check))
    assert (check / "other.yaml").read_text() == "o: 1\n"
    assert (check / "mine.yaml").read_text() == "m: 1\n"
    assert (work / "other.yaml").exists()


def test_reconcile_refuses_same_file_remote_clobber(tmp_path: Path) -> None:
    # P2.3: a concurrent REMOTE edit to a file this publish would rewrite must be
    # refused (ConcurrencyConflict), not silently reverted by re-applying the
    # stale full-file render on top of the fetched remote head.
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    assert repo.publish({"shared.yaml": "v: 1\n"}, message="first").pushed is True

    # someone hand-edits the SAME file on the remote (our clone never fetches it)
    other = tmp_path / "other"
    porcelain.clone(remote, str(other))
    (other / "shared.yaml").write_text("v: 99  # hand edit\n", encoding="utf-8")
    porcelain.add(str(other), paths=[str(other / "shared.yaml")])
    porcelain.commit(str(other), message=b"theirs", author=b"o <o@o>", committer=b"o <o@o>")
    porcelain.push(str(other), remote, f"refs/heads/{branch}".encode())

    # our stale publish rewrites the same file -> reconcile must refuse
    with pytest.raises(ConcurrencyConflict) as excinfo:
        repo.publish({"shared.yaml": "v: 2\n"}, message="mine")
    assert "shared.yaml" in str(excinfo.value)

    # the remote hand edit survives untouched (never reverted)
    check = tmp_path / "check"
    porcelain.clone(remote, str(check))
    assert (check / "shared.yaml").read_text() == "v: 99  # hand edit\n"


def test_stranded_commit_pushed_on_identical_retry(tmp_path: Path, monkeypatch) -> None:
    # P2.4: a commit that landed locally but failed to push must be pushed by the
    # next (content-identical) publish, not stranded behind a {changed:false}.
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    repo.publish({"f.yaml": "v: 1\n"}, message="first")

    real_push = repo._push_branch

    def flaky_push() -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(repo, "_push_branch", flaky_push)
    with pytest.raises(PushError):
        repo.publish({"f.yaml": "v: 2\n"}, message="second")
    stranded = repo.head_revision()

    # push works again; an IDENTICAL retry stages nothing but must push the stranded commit
    monkeypatch.setattr(repo, "_push_branch", real_push)
    res = repo.publish({"f.yaml": "v: 2\n"}, message="second")
    assert res.changed is False
    assert res.pushed is True
    assert res.commit_sha == stranded

    check = tmp_path / "check"
    porcelain.clone(remote, str(check))
    assert (check / "f.yaml").read_text() == "v: 2\n"


def test_push_rejection_beyond_fast_forward_raises(tmp_path: Path) -> None:
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "work"
    repo = GitopsRepo(local_path=str(work), repo_url=remote, branch=branch, push=True)
    repo.ensure()
    assert repo.publish({"f.yaml": "v: 1\n"}, message="first").pushed is True

    # the remote branch is force-rewritten to an unrelated root history
    alien = tmp_path / "alien"
    porcelain.init(str(alien))
    (alien / "alien.yaml").write_text("a: 1\n", encoding="utf-8")
    porcelain.add(str(alien), paths=[str(alien / "alien.yaml")])
    porcelain.commit(str(alien), message=b"root", author=b"a <a@a>", committer=b"a <a@a>")
    alien_branch = porcelain.active_branch(str(alien)).decode()
    porcelain.push(
        str(alien),
        remote,
        f"refs/heads/{alien_branch}:refs/heads/{branch}".encode(),
        force=True,
    )

    with pytest.raises(PushError) as excinfo:
        repo.publish({"g.yaml": "v: 2\n"}, message="second")

    # local commit stranded but recorded; the remote was never force-pushed
    assert excinfo.value.committed_sha == repo.head_revision()
    check = tmp_path / "check"
    porcelain.clone(remote, str(check))
    assert (check / "alien.yaml").exists()
    assert not (check / "g.yaml").exists()
