#  Project:      dfe-engine
#  File:         tests/gitcrud/test_replica_reads.py
#  Purpose:      Two engine replicas over one deploy repo agree on what exists
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two clones of one deploy repo, the shape two engine pods have (real dulwich).

Each replica clones the deploy repo into its own emptyDir, so a resource one pod
wrote is in the other pod's tree only once that pod takes the remote's head. These
tests are the read half: write on A, read on B.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from dulwich import porcelain

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.repo import GitopsRepo, read_scope
from dfe_engine.source.models import Source, SourceMatch, SourceView
from dfe_engine.source.registry import SourceNotFoundError, SourceRegistry


def _seeded_remote(tmp_path: Path) -> tuple[str, str]:
    """A bare deploy repo with one commit, and the branch it lives on."""
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


def _replica(tmp_path: Path, name: str, remote: str, branch: str) -> GitCrud:
    """One replica's own clone of the deploy repo, as the pod gets it."""
    repo = GitopsRepo(local_path=str(tmp_path / name), repo_url=remote, branch=branch, push=True)
    return GitCrud(repo)


@pytest.fixture
def replicas(tmp_path: Path) -> tuple[GitCrud, GitCrud]:
    remote, branch = _seeded_remote(tmp_path)
    return _replica(tmp_path, "a", remote, branch), _replica(tmp_path, "b", remote, branch)


def _source(name: str = "filebeat") -> Source:
    return Source(
        source=name,
        enabled=True,
        description=f"{name} test source",
        match=SourceMatch(field="tags.collector.type", value=name),
        views=[SourceView(standard="sigma", taxonomy="linux")],
    )


class TestSourcesAcrossReplicas:
    def test_a_source_created_on_one_replica_is_read_on_the_other(self, replicas) -> None:
        crud_a, crud_b = replicas
        SourceRegistry(crud=crud_a).save_source(_source("fbd83e6f40"))

        assert SourceRegistry(crud=crud_b).get_source("fbd83e6f40").source == "fbd83e6f40"

    def test_repeated_listings_agree_across_replicas(self, replicas) -> None:
        """The twelve-call probe: the same question, the same answer, either pod."""
        crud_a, crud_b = replicas
        registry_a, registry_b = SourceRegistry(crud=crud_a), SourceRegistry(crud=crud_b)
        registry_a.save_source(_source("filebeat"))
        registry_b.save_source(_source("onboard2c9ede0d"))
        registry_a.save_source(_source("fbd83e6f40"))

        expected = ["fbd83e6f40", "filebeat", "onboard2c9ede0d"]
        for registry in (registry_a, registry_b) * 6:
            assert sorted(row["source"] for row in registry.list_sources()) == expected

    def test_a_delete_lands_on_the_replica_that_never_saw_the_source(self, replicas) -> None:
        """The onboarded source that stayed deployed: created on A, deleted on B."""
        crud_a, crud_b = replicas
        registry_a, registry_b = SourceRegistry(crud=crud_a), SourceRegistry(crud=crud_b)
        registry_a.save_source(_source("onboard2c9ede0d"))

        registry_b.delete_source("onboard2c9ede0d")

        assert registry_b.source_exists("onboard2c9ede0d") is False
        assert registry_a.source_exists("onboard2c9ede0d") is False
        with pytest.raises(SourceNotFoundError):
            registry_a.get_source("onboard2c9ede0d")

    def test_an_edit_on_one_replica_is_not_clobbered_by_the_other(self, replicas) -> None:
        crud_a, crud_b = replicas
        registry_a, registry_b = SourceRegistry(crud=crud_a), SourceRegistry(crud=crud_b)
        registry_a.save_source(_source("filebeat"))
        registry_b.save_source(_source("syslog"))

        updated = _source("filebeat")
        updated.description = "edited on b"
        registry_b.save_source(updated)

        assert registry_a.get_source("filebeat").description == "edited on b"
        assert registry_a.get_source("syslog").source == "syslog"


class TestAnyResourceClassAcrossReplicas:
    """The clone is shared by every resource class, so the fix is not sources-only."""

    def test_a_doc_written_on_one_replica_is_listed_and_read_on_the_other(self, replicas) -> None:
        crud_a, crud_b = replicas
        crud_a.put("sources", "syslog", {"source": "syslog"}, actor="kaz")

        assert crud_b.list("sources") == ["syslog"]
        assert crud_b.get("sources", "syslog")["source"] == "syslog"

    def test_set_key_writes_onto_the_other_replicas_doc(self, replicas) -> None:
        crud_a, crud_b = replicas
        crud_a.put("sources", "syslog", {"source": "syslog", "enabled": True}, actor="kaz")

        crud_b.set_key("sources", "syslog", "enabled", False, actor="kay")

        assert crud_a.get("sources", "syslog") == {"source": "syslog", "enabled": False}

    def test_head_revision_is_the_deploy_repos_head_not_this_clones(self, replicas) -> None:
        crud_a, crud_b = replicas
        crud_a.put("sources", "syslog", {"source": "syslog"}, actor="kaz")

        assert crud_b.head_revision() == crud_a.head_revision()


class TestReadsHoldTheTree:
    """One working tree, many request threads: a read must not walk a tree mid-write."""

    # A publish that is deliberately parked cannot finish, so a reader that does
    # come back has walked the tree it was holding.
    _JOIN_SECONDS = 10.0
    _MUST_NOT_FINISH_SECONDS = 0.5

    @staticmethod
    def _local_crud(tmp_path: Path) -> tuple[GitopsRepo, GitCrud]:
        repo = GitopsRepo(local_path=str(tmp_path / "work"), repo_url="", branch="main", push=False)
        return repo, GitCrud(repo)

    def test_a_read_walks_the_tree_under_the_publish_lock(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        repo, crud = self._local_crud(tmp_path)
        crud.put("sources", "syslog", {"source": "syslog"}, actor="kaz")
        contended: list[bool] = []
        real_read_doc = crud._read_doc

        def probe() -> None:
            # RLock is reentrant for its owner, so the probe runs on another thread.
            taken = repo._lock.acquire(blocking=False)
            contended.append(not taken)
            if taken:
                repo._lock.release()

        def probing_read_doc(cls, name: str) -> dict:
            prober = threading.Thread(target=probe)
            prober.start()
            prober.join(timeout=self._JOIN_SECONDS)
            return real_read_doc(cls, name)

        monkeypatch.setattr(crud, "_read_doc", probing_read_doc)

        assert crud.get("sources", "syslog") == {"source": "syslog"}
        assert contended == [True]

    def test_a_listing_waits_for_a_publish_to_finish_staging(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The 404-for-a-resource-that-exists window: list while the tree is half written."""
        repo, crud = self._local_crud(tmp_path)
        crud.put("sources", "syslog", {"source": "syslog"}, actor="kaz")
        staging, finish, listed = threading.Event(), threading.Event(), threading.Event()
        names: list[list[str]] = []
        real_stage = repo._stage_and_commit

        def parked_stage(*args, **kwargs):
            staging.set()
            assert finish.wait(timeout=self._JOIN_SECONDS)
            return real_stage(*args, **kwargs)

        def read() -> None:
            names.append(crud.list("sources"))
            listed.set()

        monkeypatch.setattr(repo, "_stage_and_commit", parked_stage)
        writer = threading.Thread(
            target=crud.put, args=("sources", "filebeat", {"source": "filebeat"}, "kay")
        )
        writer.start()
        assert staging.wait(timeout=self._JOIN_SECONDS)
        reader = threading.Thread(target=read)
        reader.start()

        assert listed.wait(timeout=self._MUST_NOT_FINISH_SECONDS) is False
        finish.set()
        writer.join(timeout=self._JOIN_SECONDS)
        reader.join(timeout=self._JOIN_SECONDS)
        assert names == [["filebeat", "syslog"]]


class TestOneViewPerRequest:
    """A request reads many documents; they all answer from one view of the repo."""

    @staticmethod
    def _count_head_checks(crud: GitCrud, monkeypatch) -> list[str]:
        asked: list[str] = []
        real_remote_head = crud.repo.remote_head

        def counted() -> str | None:
            head = real_remote_head()
            asked.append(head or "")
            return head

        monkeypatch.setattr(crud.repo, "remote_head", counted)
        return asked

    def test_a_burst_of_reads_in_one_scope_asks_the_remote_once(
        self, replicas, monkeypatch
    ) -> None:
        """A listing reads every document: that is one round trip, not one each."""
        crud_a, crud_b = replicas
        crud_a.put("sources", "syslog", {"source": "syslog"}, actor="kaz")
        asked = self._count_head_checks(crud_b, monkeypatch)

        with read_scope():
            for _ in range(5):
                assert crud_b.get("sources", "syslog")["source"] == "syslog"
                assert crud_b.list("sources") == ["syslog"]

        assert len(asked) == 1

    def test_the_next_scope_asks_again(self, replicas, monkeypatch) -> None:
        """One view per request, not one per process: the next request re-checks."""
        crud_a, crud_b = replicas
        asked = self._count_head_checks(crud_b, monkeypatch)

        with read_scope():
            assert crud_b.list("sources") == []
        crud_a.put("sources", "syslog", {"source": "syslog"}, actor="kaz")
        with read_scope():
            assert crud_b.list("sources") == ["syslog"]

        assert len(asked) == 2

    def test_a_write_reopens_the_scopes_check(self, replicas, monkeypatch) -> None:
        """A replica that writes mid-request reads its own write back, not the view."""
        crud_a, crud_b = replicas
        asked = self._count_head_checks(crud_b, monkeypatch)

        with read_scope():
            assert crud_b.list("sources") == []
            crud_b.put("sources", "syslog", {"source": "syslog"}, actor="kay")
            assert crud_b.list("sources") == ["syslog"]

        assert len(asked) == 2
