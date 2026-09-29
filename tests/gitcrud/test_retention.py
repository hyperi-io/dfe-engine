#  Project:      dfe-engine
#  File:         tests/gitcrud/test_retention.py
#  Purpose:      The stored default-TTL override and the resolver every reader uses
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Stored override round-trip, precedence over the deployment default, fail-safe reads."""

from types import SimpleNamespace

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitcrud.retention import (
    MAX_DEFAULT_TTL_DAYS,
    RetentionState,
    effective_settings,
    resolve_state,
    set_stored,
    stored_days,
)
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import ClickHouseSettings, DFESettings


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _settings(days: int = 90):
    return SimpleNamespace(clickhouse=SimpleNamespace(default_ttl_days=days))


def _write(crud: GitCrud, content: str) -> None:
    path = crud.repo_path / "governance" / "settings" / "retention.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestResolve:
    def test_no_override_is_the_deployment_default(self, crud):
        assert resolve_state(crud, _settings(90)) == RetentionState(
            stored=None, effective=90, origin="deployment", deployment_default=90
        )

    def test_without_gitops_it_is_the_deployment_default(self):
        assert resolve_state(None, _settings(45)).effective == 45

    def test_a_stored_override_wins_and_is_a_commit(self, crud):
        res = set_stored(crud, 30, actor="derek")

        assert res is not None
        assert res.commit_sha
        assert crud.get("gov_settings", "retention")["default_ttl_days"] == 30
        assert resolve_state(crud, _settings(90)) == RetentionState(
            stored=30, effective=30, origin="override", deployment_default=90
        )

    def test_a_zero_override_turns_the_default_off(self, crud):
        set_stored(crud, 0, actor="derek")

        state = resolve_state(crud, _settings(90))

        assert state.effective == 0
        assert state.origin == "override"

    def test_clearing_removes_the_key(self, crud):
        set_stored(crud, 30, actor="derek")

        res = set_stored(crud, None, actor="derek")

        assert res is not None
        assert "default_ttl_days" not in crud.get("gov_settings", "retention")
        assert resolve_state(crud, _settings(90)).origin == "deployment"

    def test_clearing_nothing_commits_nothing(self, crud):
        assert set_stored(crud, None, actor="derek") is None
        assert crud.head_revision() is None

    @pytest.mark.parametrize("days", [-1, MAX_DEFAULT_TTL_DAYS + 1])
    def test_out_of_range_is_refused_before_any_commit(self, crud, days):
        with pytest.raises(ValueError):
            set_stored(crud, days, actor="derek")
        assert crud.head_revision() is None

    def test_the_bounds_themselves_are_accepted(self, crud):
        set_stored(crud, MAX_DEFAULT_TTL_DAYS, actor="derek")
        assert stored_days(crud) == MAX_DEFAULT_TTL_DAYS


class TestEffectiveSettings:
    def _deployed(self, days: int) -> DFESettings:
        return DFESettings(env="dev", clickhouse=ClickHouseSettings(default_ttl_days=days))

    def test_without_an_override_it_is_the_same_settings(self, crud):
        settings = self._deployed(90)
        assert effective_settings(settings, crud) is settings
        assert effective_settings(settings, None) is settings

    def test_an_override_is_a_copy_and_the_deployed_value_survives(self, crud):
        settings = self._deployed(90)
        set_stored(crud, 7, actor="derek")

        effective = effective_settings(settings, crud)

        assert effective.clickhouse.default_ttl_days == 7
        assert settings.clickhouse.default_ttl_days == 90
        assert effective.clickhouse.effective_data_database == (
            settings.clickhouse.effective_data_database
        )


class TestBrokenFile:
    """A hand-edited retention.yaml that is broken falls back to the deployment default."""

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
