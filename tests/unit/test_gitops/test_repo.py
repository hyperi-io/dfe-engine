"""Real-git tests for GitopsRepo (dulwich, tempdirs -- no mocks)."""

from __future__ import annotations

import io
import shutil
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from dulwich import porcelain

from dfe_engine.gitops import repo as repo_module
from dfe_engine.gitops.repo import GitopsRemoteError, GitopsRepo, PublishResult

# The hanging-forge tests must fail on the timeout, not on the machine being slow.
_HANG_TIMEOUT_SECONDS = 1.0
_HANG_BOUND_SECONDS = 10.0


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


class _RecordingLogger:
    """Stand-in for the module logger: what a run of failed refreshes actually says."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, message: str, **_fields: object) -> None:
        self.warnings.append(message)

    def info(self, message: str, **_fields: object) -> None:
        self.infos.append(message)


@pytest.fixture
def black_holed_forge(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A URL whose listener completes the handshake and then never answers.

    The shape a wedged forge has, and the one an unbounded read blocks on: the SYN
    is answered from the kernel's backlog, so the connect succeeds and the client
    waits on a reply that never comes rather than being refused.
    """
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(repo_module, "REMOTE_HEAD_TIMEOUT_SECONDS", _HANG_TIMEOUT_SECONDS)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/deploy.git"
    finally:
        listener.close()


def test_remote_head_gives_up_on_a_forge_that_never_answers(
    tmp_path: Path, black_holed_forge: str
) -> None:
    """Unbounded, this blocks for the OS connect timeout with the request open."""
    repo = GitopsRepo(
        local_path=str(tmp_path / "a"), repo_url=black_holed_forge, branch="main", push=True
    )

    started = time.monotonic()
    with pytest.raises(GitopsRemoteError):
        repo.remote_head()

    assert time.monotonic() - started < _HANG_BOUND_SECONDS


def _clone_then_point_at(tmp_path: Path, url: str) -> GitopsRepo:
    """A populated clone of a real remote, configured against ``url`` from now on."""
    remote, branch = _seeded_remote(tmp_path)
    work = tmp_path / "a"
    porcelain.clone(remote, str(work), branch=branch.encode())
    return GitopsRepo(local_path=str(work), repo_url=url, branch=branch, push=True)


def test_refresh_serves_this_clone_when_the_forge_stops_answering(
    tmp_path: Path, black_holed_forge: str
) -> None:
    """A forge that hangs is a forge blip: the read answers from what the clone has."""
    repo = _clone_then_point_at(tmp_path, black_holed_forge)
    head = repo.head_revision()

    started = time.monotonic()
    assert repo.refresh() is False

    assert time.monotonic() - started < _HANG_BOUND_SECONDS
    assert repo.head_revision() == head
    assert (tmp_path / "a" / "README").read_text() == "seed\n"


def test_a_hanging_forge_is_reported_once_per_outage_not_once_per_read(
    tmp_path: Path, black_holed_forge: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract as an unreachable forge: one warning, then quiet until it returns."""
    repo = _clone_then_point_at(tmp_path, black_holed_forge)
    recorder = _RecordingLogger()
    monkeypatch.setattr(repo_module, "logger", recorder)

    for _ in range(3):
        assert repo.refresh() is False

    assert len(recorder.warnings) == 1


def test_read_locked_holds_the_tree_lock_across_the_read(tmp_path: Path) -> None:
    """A publish resets and stages under this lock, so a reader must not walk past it."""
    repo = GitopsRepo(local_path=str(tmp_path / "work"), repo_url="", branch="main", push=False)
    repo.ensure()
    contended: list[bool] = []

    def probe() -> None:
        # RLock is reentrant for its owner, so the probe runs on another thread.
        taken = repo._lock.acquire(blocking=False)
        contended.append(not taken)
        if taken:
            repo._lock.release()

    with repo.read_locked():
        prober = threading.Thread(target=probe)
        prober.start()
        prober.join(timeout=_HANG_BOUND_SECONDS)

    assert contended == [True]


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
