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

from pathlib import Path

import pytest
from dulwich import porcelain

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.repo import GitopsRepo
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
