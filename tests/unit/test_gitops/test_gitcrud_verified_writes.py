#  Project:      dfe-engine
#  File:         tests/unit/test_gitops/test_gitcrud_verified_writes.py
#  Purpose:      A resource whose YAML does not read back never reaches a deploy-repo commit
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real dulwich against a bare remote: a refused write commits and pushes nothing."""

from enum import StrEnum
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.object_store import tree_lookup_path
from dulwich.repo import Repo

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.yaml_health import write_health
from dfe_engine.yaml_utils import YamlWriteError, yaml_dump_string


class _Colour(StrEnum):
    RED = "red"


def _remote(tmp_path: Path) -> tuple[str, str]:
    """A bare deploy repo holding one commit, and its branch."""
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


def _remote_head(remote: str, branch: str) -> bytes:
    with Repo(remote) as repo:
        return repo.refs[f"refs/heads/{branch}".encode()]


def _remote_file(remote: str, branch: str, rel: str) -> str | None:
    with Repo(remote) as repo:
        tree = repo[repo.refs[f"refs/heads/{branch}".encode()]].tree
        try:
            _mode, sha = tree_lookup_path(repo.get_object, tree, rel.encode())
        except KeyError:
            return None
        return repo[sha].data.decode("utf-8")


@pytest.fixture
def crud(tmp_path: Path):
    """A GitCrud over a pushing clone, with the bare remote it pushes to and its branch."""
    remote, branch = _remote(tmp_path)
    repo = GitopsRepo(local_path=str(tmp_path / "clone"), repo_url=remote, branch=branch, push=True)
    return GitCrud(repo), remote, branch


def _rel(crud: GitCrud, name: str) -> str:
    cls = crud.resource_class("sources")
    return f"{cls.directory}/{name}{cls.suffix}"


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param({"source": "syslog", "note": "\x85"}, id="reads-back-differently"),
        pytest.param({"source": "syslog", "colour": _Colour.RED}, id="does-not-dump"),
    ],
)
def test_a_refused_write_leaves_the_deploy_repo_as_it_was(crud, doc):
    gc, remote, branch = crud
    rel = _rel(gc, "syslog")
    gc.put("sources", "syslog", {"source": "syslog"}, actor="t")
    head = _remote_head(remote, branch)
    local = gc.head_revision()

    with pytest.raises(YamlWriteError):
        gc.put("sources", "syslog", doc, actor="t")

    assert _remote_head(remote, branch) == head
    assert gc.head_revision() == local
    assert _remote_file(remote, branch, rel) == "source: syslog\n"
    assert gc.get("sources", "syslog") == {"source": "syslog"}
    assert [e.target for e in write_health().degraded()] == [f"deploy-repo:{rel}"]


def test_every_write_in_a_many_write_commits_nothing_when_one_is_refused(crud):
    gc, remote, branch = crud
    head = _remote_head(remote, branch)

    with pytest.raises(YamlWriteError):
        gc.put_many(
            [("sources", "a", {"source": "a"}), ("sources", "b", {"note": "\x85"})],
            actor="t",
            message="two sources",
        )

    assert _remote_head(remote, branch) == head
    assert _remote_file(remote, branch, _rel(gc, "a")) is None


def test_the_next_clean_write_of_the_file_clears_it(crud):
    gc, remote, branch = crud
    rel = _rel(gc, "syslog")
    with pytest.raises(YamlWriteError):
        gc.put("sources", "syslog", {"note": "\x85"}, actor="t")

    gc.put("sources", "syslog", {"source": "syslog"}, actor="t")

    assert _remote_file(remote, branch, rel) == "source: syslog\n"
    assert write_health().degraded() == []


def test_a_failed_dump_does_not_empty_the_next_committed_file(crud):
    """The writer's open document from a failed dump once made every later dump ''."""
    gc, remote, branch = crud
    with pytest.raises(YamlWriteError):
        yaml_dump_string({"colour": _Colour.RED})

    gc.put("sources", "syslog", {"source": "syslog"}, actor="t")

    assert _remote_file(remote, branch, _rel(gc, "syslog")) == "source: syslog\n"
