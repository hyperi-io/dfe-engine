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
from dfe_engine.gitcrud.table_defaults import (
    UNSET,
    commit_patch,
    pin_table_defaults,
    resolve,
    source_default_drift,
)
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.applier import LiveTable, LiveTtl
from dfe_engine.settings import ClickHouseSettings, DFESettings
from dfe_engine.source.models import Source


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

    def test_a_header_stored_by_its_registry_path_reads_as_the_bare_name(self, crud):
        crud.put_many(
            [("gov_settings", "defaults", {"common_header_type": "common-header/minimal"})],
            "derek",
            "cfg(defaults): update table defaults",
        )

        state = resolve(crud, _settings())

        assert state.header_type == "minimal"
        assert state.header_type_stored == "minimal"
        assert state.header_type_origin == "override"


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

    def test_a_header_named_by_its_registry_path_is_stored_bare(self, crud):
        commit_patch(crud, actor="derek", common_header_type="common-header/minimal")

        assert crud.get("gov_settings", "defaults") == {"common_header_type": "minimal"}

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


def _source(name: str, *, header: dict | None = None, schema: dict | None = None) -> Source:
    version: dict = {
        "date_time": "2026-01-01",
        "match": {"field": "tags.collector.type", "value": name},
    }
    if header is not None:
        version["header"] = header
    if schema is not None:
        version["schema"] = schema
    return Source.model_validate(
        {"source": name, "current": "1.0.0", "versions": {"1.0.0": version}}
    )


class TestPinTableDefaults:
    def test_pins_the_current_version_and_leaves_its_other_fields(self):
        source = _source("syslog", schema={"meta_schema": "meta.yaml", "ttl_days": 7})

        pinned = pin_table_defaults(
            source,
            header_type="minimal",
            header_version="1.0.0",
            ttl_days=30,
            engine="ReplacingMergeTree",
        )

        assert pinned is not None
        snap = pinned.versions["1.0.0"]
        assert snap.header is not None
        assert snap.header.type == "minimal"
        assert snap.header.version == "1.0.0"
        assert snap.schema_config is not None
        assert snap.schema_config.ttl_days == 30
        assert snap.schema_config.engine == "ReplacingMergeTree"
        assert snap.schema_config.meta_schema == "meta.yaml"
        assert snap.match is not None
        assert snap.match.value == "syslog"

    def test_a_source_already_storing_the_defaults_is_left_alone(self):
        source = _source(
            "syslog",
            header={"type": "minimal", "version": "1.0.0"},
            schema={"ttl_days": 30, "engine": "ReplacingMergeTree"},
        )

        assert (
            pin_table_defaults(
                source,
                header_type="minimal",
                header_version="1.0.0",
                ttl_days=30,
                engine="ReplacingMergeTree",
            )
            is None
        )

    def test_a_header_stored_by_its_registry_path_already_matches(self):
        source = _source(
            "syslog",
            header={"type": "common-header/minimal", "version": "1.0.0"},
            schema={"ttl_days": 30, "engine": "ReplacingMergeTree"},
        )

        assert (
            pin_table_defaults(
                source,
                header_type="minimal",
                header_version="1.0.0",
                ttl_days=30,
                engine="ReplacingMergeTree",
            )
            is None
        )

    def test_the_pinned_header_is_written_bare(self):
        source = _source("syslog", schema={"meta_schema": "meta.yaml"})

        pinned = pin_table_defaults(
            source,
            header_type="common-header/minimal",
            header_version="1.0.0",
            ttl_days=30,
            engine="MergeTree",
        )

        assert pinned is not None
        header = pinned.versions["1.0.0"].header
        assert header is not None
        assert header.type == "minimal"


def _drift(source: Source, live: LiveTable | None = None):
    return source_default_drift(
        source,
        header_type="minimal",
        header_version="1.0.0",
        ttl_days=30,
        engine="ReplacingMergeTree",
        live=live,
    )


class TestSourceDefaultDrift:
    def test_an_inheriting_source_has_no_drift(self):
        assert _drift(_source("syslog", schema={"engine": ""})) is None

    def test_a_source_already_storing_the_defaults_has_no_drift(self):
        source = _source(
            "syslog",
            header={"type": "minimal", "version": "1.0.0"},
            schema={"ttl_days": 30, "engine": "ReplacingMergeTree"},
        )

        assert _drift(source) is None

    def test_a_stored_value_other_than_the_default_is_drift(self):
        source = _source(
            "syslog",
            header={"type": "timeseries", "version": "1.0.0"},
            schema={"ttl_days": 7, "engine": "MergeTree"},
        )

        report = _drift(source)

        assert report is not None
        assert report.drifted == ("ttl_days", "common_header_type", "engine")
        assert report.ttl_days.stored == 7
        assert report.ttl_days.default == 30
        assert report.common_header_type.stored == "timeseries"
        assert report.common_header_version.drifted is False
        assert report.engine.stored == "MergeTree"
        assert report.ttl_days.live is None

    def test_a_header_stored_by_its_registry_path_is_not_drift(self):
        source = _source(
            "syslog",
            header={"type": "common-header/minimal", "version": "1.0.0"},
            schema={"ttl_days": 30, "engine": "ReplacingMergeTree"},
        )

        assert _drift(source) is None

    def test_a_deployed_table_is_measured_rather_than_what_the_source_stores(self):
        source = _source(
            "syslog",
            header={"type": "minimal", "version": "1.0.0"},
            schema={"ttl_days": 30, "engine": "ReplacingMergeTree"},
        )

        report = _drift(source, LiveTable(engine="MergeTree", ttl=LiveTtl(7, "day")))

        assert report is not None
        assert report.drifted == ("ttl_days", "engine")
        assert report.ttl_days.stored == 30
        assert report.ttl_days.live == 7
        assert report.engine.stored == "ReplacingMergeTree"
        assert report.engine.live == "MergeTree"
        assert report.common_header_type.live is None

    def test_a_table_on_the_defaults_has_no_drift_whatever_its_topology(self):
        source = _source("syslog", schema={"engine": ""})

        live = LiveTable(engine="ReplicatedReplacingMergeTree", ttl=LiveTtl(30, "day"))
        assert _drift(source, live) is None

    def test_a_table_with_no_ttl_runs_zero_days(self):
        source = _source("syslog", schema={"engine": ""})

        report = _drift(source, LiveTable(engine="ReplacingMergeTree", ttl=None))

        assert report is not None
        assert report.drifted == ("ttl_days",)
        assert report.ttl_days.live == 0

    def test_a_ttl_that_is_not_whole_days_is_reported_as_its_interval(self):
        source = _source("syslog", schema={"engine": ""})

        report = _drift(source, LiveTable(engine="ReplacingMergeTree", ttl=LiveTtl(6, "hour")))

        assert report is not None
        assert report.drifted == ("ttl_days",)
        assert report.ttl_days.live == "6 hour"

    def test_a_ttl_in_whole_days_by_another_unit_matches(self):
        source = _source("syslog", schema={"engine": ""})

        live = LiveTable(engine="ReplacingMergeTree", ttl=LiveTtl(720, "hour"))
        assert _drift(source, live) is None
