#  Project:      dfe-engine
#  File:         tests/gitcrud/test_if_match_race.py
#  Purpose:      A write sent with If-Match never lands over a replica's write that beat it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two replicas, one deploy repo, one doc, real dulwich clones.

The other replica's write lands at the two points a stale write used to get
through: after the If-Match guard read the head and before the write fetched,
and between the write's fetch and its push.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud
from dfe_engine.gitops.repo import GitopsRepo
from tests.support.deploy_remote import seeded_remote

_OURS = {"source": "syslog", "enabled": True}
_THEIRS = {"source": "syslog", "enabled": True, "description": "edited on a"}


def _replica(tmp_path: Path, name: str, remote: Path, branch: str) -> GitCrud:
    """One replica's own clone of the deploy repo, as the pod gets it."""
    repo = GitopsRepo(
        local_path=str(tmp_path / name), repo_url=str(remote), branch=branch, push=True
    )
    return GitCrud(repo)


@pytest.fixture
def replicas(tmp_path: Path) -> tuple[GitCrud, GitCrud]:
    remote, branch = seeded_remote(tmp_path)
    return _replica(tmp_path, "a", remote, branch), _replica(tmp_path, "b", remote, branch)


def _race_before(
    monkeypatch: pytest.MonkeyPatch,
    repo: GitopsRepo,
    method: str,
    race: Callable[[], Any],
    *,
    when: Callable[[dict[str, Any]], bool] = lambda _kwargs: True,
) -> None:
    """Run *race* once, just before the first matching call to *method* on *repo*."""
    real = getattr(repo, method)
    raced: list[bool] = []

    def racing(*args: Any, **kwargs: Any) -> Any:
        if not raced and when(kwargs):
            raced.append(True)
            race()
        return real(*args, **kwargs)

    monkeypatch.setattr(repo, method, racing)


def _theirs_lands(crud: GitCrud) -> Callable[[], Any]:
    return lambda: crud.put("sources", "syslog", _THEIRS, actor="kaz")


def test_an_if_match_write_whose_push_lost_the_race_conflicts(replicas, monkeypatch):
    crud_a, crud_b = replicas
    crud_a.put("sources", "syslog", _OURS, actor="kaz")
    _, base = crud_b.get_with_revision("sources", "syslog")
    _race_before(monkeypatch, crud_b.repo, "_push_refspec", _theirs_lands(crud_a))

    with pytest.raises(ConcurrencyConflictError) as raised:
        crud_b.set_key("sources", "syslog", "enabled", False, actor="kay", base_revision=base)

    assert raised.value.current == _THEIRS
    assert raised.value.head == crud_a.head_revision()
    assert crud_a.get("sources", "syslog") == _THEIRS
    assert crud_b.get("sources", "syslog") == _THEIRS


def test_an_if_match_write_in_a_batch_whose_push_lost_the_race_conflicts(replicas, monkeypatch):
    crud_a, crud_b = replicas
    crud_a.put("sources", "syslog", _OURS, actor="kaz")
    _, base = crud_b.get_with_revision("sources", "syslog")
    _race_before(monkeypatch, crud_b.repo, "_push_refspec", _theirs_lands(crud_a))

    with pytest.raises(ConcurrencyConflictError) as raised, crud_b.batch("edit sources"):
        crud_b.set_key("sources", "syslog", "enabled", False, actor="kay", base_revision=base)

    assert (raised.value.cls, raised.value.name) == ("sources", "syslog")
    assert raised.value.current == _THEIRS
    assert crud_a.get("sources", "syslog") == _THEIRS


def test_an_if_match_write_the_remote_moved_under_before_its_commit_conflicts(
    replicas, monkeypatch
):
    crud_a, crud_b = replicas
    crud_a.put("sources", "syslog", _OURS, actor="kaz")
    _, base = crud_b.get_with_revision("sources", "syslog")
    _race_before(
        monkeypatch,
        crud_b.repo,
        "_fetch_head",
        _theirs_lands(crud_a),
        when=lambda kwargs: kwargs.get("write") is True,
    )

    with pytest.raises(ConcurrencyConflictError) as raised:
        crud_b.put(
            "sources",
            "syslog",
            {"source": "syslog", "enabled": False},
            actor="kay",
            base_revision=base,
        )

    assert raised.value.current == _THEIRS
    assert crud_a.get("sources", "syslog") == _THEIRS


def test_a_delete_sent_with_if_match_does_not_remove_a_doc_edited_under_it(replicas, monkeypatch):
    crud_a, crud_b = replicas
    crud_a.put("sources", "syslog", _OURS, actor="kaz")
    _, base = crud_b.get_with_revision("sources", "syslog")
    _race_before(monkeypatch, crud_b.repo, "_push_refspec", _theirs_lands(crud_a))

    with pytest.raises(ConcurrencyConflictError):
        crud_b.delete("sources", "syslog", actor="kay", base_revision=base)

    assert crud_a.get("sources", "syslog") == _THEIRS


def test_a_write_without_if_match_still_lands_on_the_head_that_beat_it(replicas, monkeypatch):
    crud_a, crud_b = replicas
    crud_a.put("sources", "syslog", _OURS, actor="kaz")
    crud_b.get("sources", "syslog")
    _race_before(monkeypatch, crud_b.repo, "_push_refspec", _theirs_lands(crud_a))

    result = crud_b.set_key("sources", "syslog", "enabled", False, actor="kay")

    assert result.pushed is True
    assert crud_a.get("sources", "syslog")["enabled"] is False
