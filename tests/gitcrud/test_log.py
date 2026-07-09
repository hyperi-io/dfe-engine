"""Gitcrud audit log: history walk, commit-standard parsing, grouping, state."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.commit_policy import CommitContext, build_message
from dfe_engine.gitcrud.log import UnknownCursorError, group_log, read_log
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _commit_var(crud, name, path, value, actor):
    msg = build_message(CommitContext(ctype="cfg", scope=name, summary=f"set {path}", actor=actor))
    return crud.set_key("helmvars", name, path, value, actor, message=msg)


class TestReadLog:
    def test_empty_repo(self, crud):
        entries, next_before = read_log(crud)
        assert entries == []
        assert next_before is None

    def test_entries_newest_first_and_parsed(self, crud):
        _commit_var(crud, "receiver-default", "keda.maxReplicas", 5, "derek")
        _commit_var(crud, "loader-default", "keda.maxReplicas", 7, "kaz")
        entries, _ = read_log(crud)
        assert len(entries) == 2
        newest, oldest = entries
        assert newest.scope == "loader-default"
        assert newest.actor == "kaz"
        assert newest.ctype == "cfg"
        assert newest.conforming is True
        assert newest.state == "committed"
        assert "values/loader-default.yaml" in newest.files
        assert "helmvars/loader-default" in newest.resources
        assert oldest.scope == "receiver-default"
        assert newest.timestamp >= oldest.timestamp

    def test_non_conforming_commit_still_listed(self, crud):
        # a hand-made commit that ignores the standard MUST still appear
        crud._repo.publish({"values/x.yaml": "a: 1\n"}, "hand edit, no standard")
        entries, _ = read_log(crud)
        assert len(entries) == 1
        assert entries[0].conforming is False
        assert entries[0].summary == "hand edit, no standard"
        assert entries[0].actor  # falls back to the git author

    def test_cursor_pagination(self, crud):
        for i in range(5):
            _commit_var(crud, "receiver-default", "keda.maxReplicas", i, "derek")
        page1, cursor = read_log(crud, limit=2)
        assert len(page1) == 2
        assert cursor == page1[-1].sha
        page2, _ = read_log(crud, limit=2, before=cursor)
        assert len(page2) == 2
        assert {e.sha for e in page1}.isdisjoint({e.sha for e in page2})

    def test_unknown_cursor_raises(self, crud):
        _commit_var(crud, "receiver-default", "keda.maxReplicas", 1, "derek")
        with pytest.raises(UnknownCursorError):
            read_log(crud, before="deadbeef" * 5)

    def test_applied_vs_pending(self, crud):
        first = _commit_var(crud, "receiver-default", "keda.maxReplicas", 1, "derek")
        _commit_var(crud, "receiver-default", "keda.maxReplicas", 2, "derek")
        entries, _ = read_log(crud, applied_revision=first.commit_sha)
        assert entries[0].state == "pending"  # newest, after the applied SHA
        assert entries[1].state == "applied"


class TestGroupLog:
    def test_group_by_actor(self, crud):
        _commit_var(crud, "receiver-default", "keda.maxReplicas", 1, "derek")
        _commit_var(crud, "loader-default", "keda.maxReplicas", 2, "kaz")
        _commit_var(crud, "receiver-default", "keda.minReplicas", 1, "derek")
        entries, _ = read_log(crud)
        groups = group_log(entries, "actor")
        by_key = {g["key"]: g for g in groups}
        assert by_key["derek"]["count"] == 2
        assert by_key["kaz"]["count"] == 1
        # latest is the newest entry in the bucket
        assert by_key["derek"]["latest"].summary == "set keda.minReplicas"

    def test_group_by_day_and_type(self, crud):
        _commit_var(crud, "receiver-default", "keda.maxReplicas", 1, "derek")
        entries, _ = read_log(crud)
        assert group_log(entries, "day")[0]["count"] == 1
        assert group_log(entries, "type")[0]["key"] == "cfg"

    def test_unknown_group_key_rejected(self, crud):
        with pytest.raises(ValueError):
            group_log([], "nope")
