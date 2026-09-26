#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_repo_batch.py
#  Purpose:      GitopsRepo.batch lands many writes as one commit and one push
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real dulwich against a bare remote: what a batch commits, pushes, discards and splits."""

from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.object_store import tree_lookup_path
from dulwich.repo import Repo
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.metrics import BATCHES, GitopsMetrics
from dfe_engine.gitops.repo import GitopsRepo


def _remote(tmp_path: Path) -> tuple[str, str]:
    """A bare deploy repo holding one committed file, and its branch."""
    remote = tmp_path / "remote.git"
    porcelain.init(str(remote), bare=True)
    seed = tmp_path / "seed"
    porcelain.clone(str(remote), str(seed))
    (seed / "kept.yaml").write_text("kept: 1\n", encoding="utf-8")
    porcelain.add(str(seed), paths=[str(seed / "kept.yaml")])
    porcelain.commit(str(seed), message=b"init", author=b"t <t@t>", committer=b"t <t@t>")
    branch = porcelain.active_branch(str(seed)).decode()
    porcelain.push(str(seed), str(remote), f"refs/heads/{branch}".encode())
    return str(remote), branch


def _commits(remote: str, branch: str) -> list[bytes]:
    """The remote branch's commit messages, newest first."""
    with Repo(remote) as repo:
        head = repo.refs[f"refs/heads/{branch}".encode()]
        return [entry.commit.message for entry in repo.get_walker(include=[head])]


def _remote_file(remote: str, branch: str, rel: str) -> str | None:
    """A file's content at the remote branch's head, or None when it is absent there."""
    with Repo(remote) as repo:
        tree = repo[repo.refs[f"refs/heads/{branch}".encode()]].tree
        try:
            _mode, sha = tree_lookup_path(repo.get_object, tree, rel.encode())
        except KeyError:
            return None
        return repo[sha].data.decode("utf-8")


def _manager():
    return create_metrics("test", backend="prometheus", enable_auto_update=False)


def _batches(manager, outcome: str) -> float:
    """The batch counter's value for *outcome*, read off the real exposition."""
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == BATCHES and sample.labels.get("outcome") == outcome:
                return sample.value
    return 0.0


@pytest.fixture
def pushing(tmp_path: Path):
    """A pushing clone over a bare remote, with a real metrics backend behind it."""
    remote, branch = _remote(tmp_path)
    manager = _manager()
    repo = GitopsRepo(
        local_path=str(tmp_path / "clone"),
        repo_url=remote,
        branch=branch,
        push=True,
        metrics=GitopsMetrics(manager),
    )
    repo.ensure()
    return repo, remote, branch, manager


class TestOneCommit:
    def test_every_write_in_the_block_lands_as_one_commit(self, pushing) -> None:
        repo, remote, branch, manager = pushing
        before = len(_commits(remote, branch))

        with repo.batch("e2e: seed_many"):
            for i in range(5):
                repo.publish({f"values/a{i}.yaml": f"a: {i}\n"}, f"write a{i}")
            repo.publish({}, "drop kept", deletions=["kept.yaml"])

        messages = _commits(remote, branch)
        assert len(messages) == before + 1
        assert messages[0].startswith(b"e2e: seed_many\n\n- write a0\n")
        assert b"- drop kept" in messages[0]
        assert _remote_file(remote, branch, "values/a4.yaml") == "a: 4\n"
        assert _remote_file(remote, branch, "kept.yaml") is None
        assert _batches(manager, "committed") == 1

    def test_nothing_reaches_the_remote_until_the_block_ends(self, pushing) -> None:
        repo, remote, branch, _manager = pushing
        crud = GitCrud(repo)
        before = len(_commits(remote, branch))

        with repo.batch("e2e: staged"):
            crud.put("sources", "syslog", {"source": "syslog"}, actor="t")
            # Read back inside the block, and still absent on the remote.
            assert crud.get("sources", "syslog") == {"source": "syslog"}
            assert crud.list("sources") == ["syslog"]
            assert len(_commits(remote, branch)) == before

        assert len(_commits(remote, branch)) == before + 1

    def test_a_write_that_changes_nothing_reports_unchanged(self, pushing) -> None:
        repo, remote, branch, manager = pushing
        before = len(_commits(remote, branch))

        with repo.batch("e2e: noop"):
            first = repo.publish({"kept.yaml": "kept: 1\n"}, "same content")
            second = repo.publish({"values/new.yaml": "n: 1\n"}, "new file")

        assert (first.changed, second.changed) == (False, True)
        assert len(_commits(remote, branch)) == before + 1

        with repo.batch("e2e: empty"):
            repo.publish({"kept.yaml": "kept: 1\n"}, "same again")

        assert len(_commits(remote, branch)) == before + 1
        assert _batches(manager, "unchanged") == 1

    def test_a_file_written_then_removed_in_one_block_is_removed(self, pushing) -> None:
        """porcelain.remove refuses a staged-modified file, and the batch must not."""
        repo, remote, branch, _manager = pushing

        with repo.batch("e2e: rewrite then drop"):
            repo.publish({"kept.yaml": "kept: 2\n"}, "edit kept")
            repo.publish({}, "drop kept", deletions=["kept.yaml"])

        assert _remote_file(remote, branch, "kept.yaml") is None

    def test_a_file_removed_then_written_in_one_block_is_written(self, pushing) -> None:
        repo, remote, branch, _manager = pushing

        with repo.batch("e2e: drop then rewrite"):
            repo.publish({}, "drop kept", deletions=["kept.yaml"])
            repo.publish({"kept.yaml": "kept: 3\n"}, "restore kept")

        assert _remote_file(remote, branch, "kept.yaml") == "kept: 3\n"

    def test_a_nested_block_joins_the_outer_one(self, pushing) -> None:
        repo, remote, branch, _manager = pushing
        before = len(_commits(remote, branch))

        with repo.batch("e2e: outer"):
            repo.publish({"values/o.yaml": "o: 1\n"}, "outer write")
            with repo.batch("e2e: inner"):
                repo.publish({"values/i.yaml": "i: 1\n"}, "inner write")

        messages = _commits(remote, branch)
        assert len(messages) == before + 1
        assert messages[0].startswith(b"e2e: outer")


class TestDiscard:
    def test_an_error_in_the_block_leaves_the_repo_as_it_was(self, pushing) -> None:
        repo, remote, branch, manager = pushing
        before = _commits(remote, branch)
        head = repo.head_revision()

        with pytest.raises(RuntimeError, match="seed refused"):
            with repo.batch("e2e: refused"):
                repo.publish({"values/half.yaml": "h: 1\n"}, "half a seed")
                repo.publish({}, "drop kept", deletions=["kept.yaml"])
                raise RuntimeError("seed refused")

        assert _commits(remote, branch) == before
        assert repo.head_revision() == head
        assert not (repo.path / "values" / "half.yaml").exists()
        assert (repo.path / "kept.yaml").read_text(encoding="utf-8") == "kept: 1\n"
        assert GitCrud(repo).list("sources") == []
        assert _batches(manager, "discarded") == 1

    def test_an_error_before_any_commit_empties_the_tree(self, tmp_path: Path) -> None:
        """A repo with no commit yet has nothing to reset onto."""
        repo = GitopsRepo(local_path=str(tmp_path / "fresh"), push=False)
        repo.ensure()

        with pytest.raises(RuntimeError):
            with repo.batch("e2e: first"):
                repo.publish({"values/x.yaml": "x: 1\n"}, "first write")
                raise RuntimeError("boom")

        assert repo.head_revision() is None
        assert not (repo.path / "values" / "x.yaml").exists()
        assert porcelain.status(str(repo.path)).staged == {"add": [], "delete": [], "modify": []}

    def test_the_next_publish_after_a_discard_commits_only_its_own_write(self, pushing) -> None:
        repo, remote, branch, _manager = pushing

        with pytest.raises(RuntimeError):
            with repo.batch("e2e: refused"):
                repo.publish({"values/half.yaml": "h: 1\n"}, "half a seed")
                raise RuntimeError("seed refused")
        repo.publish({"values/after.yaml": "a: 1\n"}, "after")

        assert _remote_file(remote, branch, "values/after.yaml") == "a: 1\n"
        assert _remote_file(remote, branch, "values/half.yaml") is None


class TestAgainstAMovingRemote:
    def test_a_replica_that_pushed_during_the_block_keeps_its_write(
        self, pushing, tmp_path: Path
    ) -> None:
        repo, remote, branch, _manager = pushing
        other = GitopsRepo(
            local_path=str(tmp_path / "other"), repo_url=remote, branch=branch, push=True
        )
        other.ensure()

        with repo.batch("e2e: raced"):
            repo.publish({"values/mine.yaml": "m: 1\n"}, "mine")
            other.publish({"values/theirs.yaml": "t: 1\n"}, "theirs")

        assert _remote_file(remote, branch, "values/mine.yaml") == "m: 1\n"
        assert _remote_file(remote, branch, "values/theirs.yaml") == "t: 1\n"

    def test_no_read_in_the_block_asks_the_remote(self, pushing, monkeypatch) -> None:
        """A refresh resets the tree, which would throw away what the block staged."""
        repo, _remote_url, _branch, _manager = pushing
        crud = GitCrud(repo)
        asked: list[str] = []
        real = repo.remote_head

        def counted() -> str | None:
            asked.append("ls-remote")
            return real()

        monkeypatch.setattr(repo, "remote_head", counted)

        with repo.batch("e2e: reads"):
            crud.put("sources", "syslog", {"source": "syslog"}, actor="t")
            for _ in range(3):
                assert crud.list("sources") == ["syslog"]

        assert asked == []


class TestReviewBranchSplitsTheBatch:
    def test_a_branch_write_lands_what_the_block_holds_first(self, pushing) -> None:
        repo, remote, branch, manager = pushing

        with repo.batch("e2e: split"):
            repo.publish({"values/before.yaml": "b: 1\n"}, "before the branch")
            routed = repo.publish({"values/review.yaml": "r: 1\n"}, "for review", branch="rev-1")
            repo.publish({"values/after.yaml": "a: 1\n"}, "after the branch")

        assert routed.branch == "rev-1"
        assert _remote_file(remote, branch, "values/before.yaml") == "b: 1\n"
        assert _remote_file(remote, branch, "values/after.yaml") == "a: 1\n"
        assert _remote_file(remote, branch, "values/review.yaml") is None
        assert _remote_file(remote, "rev-1", "values/before.yaml") == "b: 1\n"
        assert _remote_file(remote, "rev-1", "values/review.yaml") == "r: 1\n"
        assert _batches(manager, "split") == 1
        assert _batches(manager, "committed") == 1


class TestMetricsWithoutABackend:
    def test_no_manager_records_nothing(self) -> None:
        metrics = GitopsMetrics()

        assert metrics.enabled is False
        metrics.batch("discarded")
