#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_reads_during_push.py
#  Purpose:      A read waits for a write's commit, never for its push
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real dulwich, two clones over one bare remote, and a push parked on purpose.

The push is the slow half of a write against a remote forge. A read must not queue
behind it, except where the remote holds a commit the reading clone has never seen:
taking that commit resets the tree, and a reset between a commit and its push would
drop the commit.
"""

import threading
from pathlib import Path

from dulwich import porcelain

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.repo import GitopsRepo

# A parked push never finishes on its own, so a reader that returns did not wait for it.
_JOIN_SECONDS = 10.0
_MUST_NOT_FINISH_SECONDS = 0.5


def _remote(tmp_path: Path) -> tuple[str, str]:
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    seed = tmp_path / "seed"
    porcelain.clone(str(remote), str(seed))
    (seed / "README").write_text("seed\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "README")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(remote), f"refs/heads/{branch}".encode())
    return str(remote), branch


def _clone(tmp_path: Path, name: str, remote: str, branch: str) -> GitCrud:
    return GitCrud(
        GitopsRepo(local_path=str(tmp_path / name), repo_url=remote, branch=branch, push=True)
    )


class _ParkedPush:
    """Holds the first push on a clone until released, then lets every push through."""

    def __init__(self, repo: GitopsRepo, monkeypatch) -> None:
        self.parked = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._real = repo._push_refspec
        monkeypatch.setattr(repo, "_push_refspec", self._push)

    def _push(self, refspec: bytes) -> None:
        if self._first:
            self._first = False
            self.parked.set()
            assert self.release.wait(timeout=_JOIN_SECONDS * 3)
        self._real(refspec)


def test_a_read_carries_on_while_a_push_is_in_flight(tmp_path: Path, monkeypatch) -> None:
    remote, branch = _remote(tmp_path)
    crud = _clone(tmp_path, "a", remote, branch)
    push = _ParkedPush(crud.repo, monkeypatch)
    writer = threading.Thread(
        target=crud.put, args=("sources", "syslog", {"source": "syslog"}, "kaz"), daemon=True
    )
    listed: list[list[str]] = []

    writer.start()
    assert push.parked.wait(timeout=_JOIN_SECONDS)
    reader = threading.Thread(target=lambda: listed.append(crud.list("sources")), daemon=True)
    reader.start()
    reader.join(timeout=_JOIN_SECONDS)
    try:
        # The commit is made, so the read sees the write before its push lands.
        assert listed == [["syslog"]]
    finally:
        push.release.set()
        writer.join(timeout=_JOIN_SECONDS)

    assert _clone(tmp_path, "check", remote, branch).list("sources") == ["syslog"]


def test_a_read_that_must_take_another_replicas_commit_waits_for_the_push(
    tmp_path: Path, monkeypatch
) -> None:
    """Taking the other replica's head mid-push would drop this clone's commit."""
    remote, branch = _remote(tmp_path)
    crud_a = _clone(tmp_path, "a", remote, branch)
    crud_b = _clone(tmp_path, "b", remote, branch)
    push = _ParkedPush(crud_a.repo, monkeypatch)
    writer = threading.Thread(
        target=crud_a.put, args=("sources", "mine", {"source": "mine"}, "kaz"), daemon=True
    )
    listed: list[list[str]] = []

    writer.start()
    assert push.parked.wait(timeout=_JOIN_SECONDS)
    crud_b.put("sources", "theirs", {"source": "theirs"}, actor="kay")
    reader = threading.Thread(target=lambda: listed.append(crud_a.list("sources")), daemon=True)
    reader.start()
    reader.join(timeout=_MUST_NOT_FINISH_SECONDS)
    assert listed == []

    push.release.set()
    writer.join(timeout=_JOIN_SECONDS)
    reader.join(timeout=_JOIN_SECONDS)

    # A's push lost the race, re-applied on B's head, and the read took both.
    assert listed == [["mine", "theirs"]]
    assert _clone(tmp_path, "check", remote, branch).list("sources") == ["mine", "theirs"]


def test_a_nested_read_does_not_wait_on_a_writer_waiting_on_it(tmp_path: Path) -> None:
    """The writer holds its lock and waits for the tree, which the outer read holds."""
    remote, branch = _remote(tmp_path)
    crud_a = _clone(tmp_path, "a", remote, branch)
    crud_b = _clone(tmp_path, "b", remote, branch)
    repo = crud_a.repo
    writer_waiting = threading.Event()
    read: list[list[str]] = []

    def writer() -> None:
        with repo._write_lock:
            writer_waiting.set()
            with repo._lock:
                pass

    def nested_read() -> None:
        with crud_a.reading():
            # The remote moves after the outer block took its head.
            crud_b.put("sources", "theirs", {"source": "theirs"}, actor="kay")
            threading.Thread(target=writer, daemon=True).start()
            assert writer_waiting.wait(timeout=_JOIN_SECONDS)
            read.append(crud_a.list("sources"))

    reader = threading.Thread(target=nested_read, daemon=True)
    reader.start()
    reader.join(timeout=_JOIN_SECONDS)

    # Served from the head the outer block took, rather than deadlocked.
    assert read == [[]]
