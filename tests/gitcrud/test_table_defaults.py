#  Project:      dfe-engine
#  File:         tests/gitcrud/test_table_defaults.py
#  Purpose:      Stored table defaults and the settings copy a deploy builds with
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A patch stores header and engine beside the TTL override, and a deploy sees them."""

import pytest

from dfe_engine.gitcrud import GitCrud
from dfe_engine.gitcrud.registry import default_registry
from dfe_engine.gitcrud.retention import effective_settings, set_stored, stored_days
from dfe_engine.gitcrud.table_defaults import UNSET, commit_patch, resolve
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.settings import ClickHouseSettings, DFESettings


@pytest.fixture
def crud(tmp_path) -> GitCrud:
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    return GitCrud(repo, default_registry())


def _settings(*, days: int = 90, engine: str = "MergeTree") -> DFESettings:
    return DFESettings(
        env="dev",
        clickhouse=ClickHouseSettings(default_ttl_days=days, default_engine=engine),
    )


class TestResolve:
    def test_without_a_file_the_deployment_values_are_the_whole_answer(self, crud):
        state = resolve(crud, _settings())

        assert state.header_type == "timeseries"
        assert state.header_type_stored is None
        assert state.header_type_origin == "deployment"
        assert state.header_version == "1.0.0"
        assert state.header_version_stored is None
        assert state.engine == "MergeTree"
        assert state.engine_stored is None
        assert state.engine_origin == "deployment"

    def test_without_gitops_there_is_nothing_to_read(self):
        state = resolve(None, _settings(engine="SummingMergeTree"))

        assert state.engine == "SummingMergeTree"
        assert state.engine_stored is None
        assert state.header_type_stored is None


class TestCommitPatch:
    def test_one_field_leaves_the_others_and_the_ttl_file_alone(self, crud):
        set_stored(crud, 30, actor="derek")

        commit_patch(crud, actor="derek", engine="ReplacingMergeTree")

        state = resolve(crud, _settings())
        assert state.engine == "ReplacingMergeTree"
        assert state.engine_stored == "ReplacingMergeTree"
        assert state.engine_origin == "override"
        assert state.header_type_stored is None
        assert stored_days(crud) == 30

    def test_null_clears_one_key_and_keeps_the_rest(self, crud):
        commit_patch(
            crud,
            actor="derek",
            common_header_type="minimal",
            common_header_version="1.0.0",
            engine="ReplacingMergeTree",
        )

        commit_patch(crud, actor="derek", engine=None)

        state = resolve(crud, _settings())
        assert state.engine_stored is None
        assert state.engine == "MergeTree"
        assert state.header_type_stored == "minimal"
        assert state.header_version_stored == "1.0.0"

    def test_an_unchanged_patch_commits_nothing(self, crud):
        assert commit_patch(crud, actor="derek") is None
        assert crud.head_revision() is None

    def test_ttl_lands_in_the_retention_override(self, crud):
        commit_patch(crud, actor="derek", ttl_days=14)

        assert stored_days(crud) == 14
        assert resolve(crud, _settings()).header_type_stored is None

    def test_clearing_ttl_removes_the_retention_override(self, crud):
        set_stored(crud, 14, actor="derek")

        commit_patch(crud, actor="derek", ttl_days=None)

        assert stored_days(crud) is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"engine": "Log"},
            {"engine": ""},
            {"common_header_type": "no-such-profile"},
            {"common_header_type": "minimal", "common_header_version": "9.9.9"},
            {"ttl_days": -1},
        ],
    )
    def test_an_unusable_value_commits_nothing(self, crud, kwargs):
        with pytest.raises(ValueError):
            commit_patch(crud, actor="derek", **kwargs)
        assert crud.head_revision() is None

    def test_unset_is_not_a_clear(self, crud):
        commit_patch(crud, actor="derek", engine="ReplacingMergeTree")

        commit_patch(crud, actor="derek", common_header_type="minimal", engine=UNSET)

        state = resolve(crud, _settings())
        assert state.engine_stored == "ReplacingMergeTree"
        assert state.header_type_stored == "minimal"


class TestEffectiveSettings:
    def test_header_and_engine_overrides_ride_the_copy_a_deploy_builds_with(self, crud):
        settings = _settings()
        commit_patch(
            crud,
            actor="derek",
            common_header_type="minimal",
            common_header_version="1.0.0",
            engine="ReplacingMergeTree",
        )

        effective = effective_settings(settings, crud)

        assert effective is not settings
        assert effective.clickhouse.default_engine == "ReplacingMergeTree"
        assert effective.clickhouse.default_header_type == "minimal"
        assert effective.clickhouse.default_header_version == "1.0.0"
        assert settings.clickhouse.default_engine == "MergeTree"
        assert settings.clickhouse.default_header_type == "timeseries"

    def test_a_ttl_only_override_does_not_move_header_or_engine(self, crud):
        settings = _settings()
        set_stored(crud, 7, actor="derek")

        effective = effective_settings(settings, crud)

        assert effective.clickhouse.default_ttl_days == 7
        assert effective.clickhouse.default_engine == "MergeTree"
        assert effective.clickhouse.default_header_type == "timeseries"
