#  Project:      dfe-engine
#  File:         tests/gitcrud/test_retention.py
#  Purpose:      The stored default-TTL override and the one resolver over it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Stored override round-trip, precedence over the env default, and fail-safe reads."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitcrud.retention import (
    RetentionState,
    effective_default_ttl_days,
    resolve_state,
    set_stored,
    stored_days,
)
from dfe_engine.gitops.repo import GitopsRepo


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _settings(days: int = 90):
    return SimpleNamespace(clickhouse=SimpleNamespace(default_ttl_days=days))


def _write(crud: GitCrud, content: str) -> None:
    path = crud.repo_path / "governance" / "settings" / "retention.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestResolve:
    def test_no_override_is_the_deployment_default(self, crud):
        assert resolve_state(crud, _settings(90)) == RetentionState(
            stored=None, effective=90, origin="deployment"
        )

    def test_stored_override_wins(self, crud):
        res = set_stored(crud, 30, actor="derek")
        assert res is not None
        assert res.commit_sha
        assert crud.get("gov_settings", "retention")["default_ttl_days"] == 30
        assert resolve_state(crud, _settings(90)) == RetentionState(
            stored=30, effective=30, origin="override"
        )

    def test_zero_override_disables_the_default(self, crud):
        set_stored(crud, 0, actor="derek")
        state = resolve_state(crud, _settings(90))
        assert state.effective == 0
        assert state.origin == "override"

    def test_clear_removes_the_key(self, crud):
        set_stored(crud, 30, actor="derek")
        res = set_stored(crud, None, actor="derek")
        assert res is not None
        assert "default_ttl_days" not in crud.get("gov_settings", "retention")
        assert resolve_state(crud, _settings(90)).origin == "deployment"

    def test_clearing_nothing_is_a_noop(self, crud):
        assert set_stored(crud, None, actor="derek") is None
        assert crud.head_revision() is None

    def test_negative_is_refused(self, crud):
        with pytest.raises(ValueError):
            set_stored(crud, -1, actor="derek")


class TestResolver:
    def test_without_gitops_it_is_the_env_value(self):
        assert effective_default_ttl_days(_settings(45), None) == 45

    def test_with_gitops_the_override_wins(self, crud):
        assert effective_default_ttl_days(_settings(45), crud) == 45
        set_stored(crud, 7, actor="derek")
        assert effective_default_ttl_days(_settings(45), crud) == 7


class TestBrokenFile:
    """A hand-edited retention.yaml that is broken falls back to the env default."""

    def test_malformed_yaml(self, crud):
        _write(crud, "[not: a mapping")
        assert stored_days(crud) is None

    def test_list_doc(self, crud):
        _write(crud, "- one\n- two\n")
        assert stored_days(crud) is None

    @pytest.mark.parametrize("value", ["'30'", "-1", "true", "12.5"])
    def test_non_int_or_negative_value(self, crud, value):
        _write(crud, f"default_ttl_days: {value}\n")
        assert stored_days(crud) is None
        assert resolve_state(crud, _settings(90)).effective == 90
