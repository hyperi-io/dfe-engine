#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_core_sources.py
#  Purpose:      The engine-owned landing source: seeding from dfe-schemas and the write gates
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the core landing source."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from dfe_engine.schema.core_schema import landing_table_ddl
from dfe_engine.source.core_sources import (
    LANDING_SOURCE_FILE,
    SOURCES_SUBDIR,
    seed_core_sources,
)
from dfe_engine.source.deployment import SourceDeploymentStore
from dfe_engine.source.registry import SourceCoreResourceError, SourceRegistry
from dfe_engine.yaml_utils import yaml_dump
from tests.support.core_sources import LANDING_DEFINITION, write_landing_definition


def _settings(*, root: Path, seeded: bool = True, table: str = "main") -> SimpleNamespace:
    """Settings the seed reads, with the definition written where the schema seed puts it."""
    schemas_dir = root / "schemas"
    if seeded:
        write_landing_definition(schemas_dir=schemas_dir)
    return SimpleNamespace(
        clickhouse=SimpleNamespace(
            default_table_profile="timeseries",
            default_ttl_days=0,
            effective_data_database="dfe",
            landing_table=table,
        ),
        schemas=SimpleNamespace(schemas_dir=str(schemas_dir)),
        source=SimpleNamespace(
            builds_dir=str(root / "source-builds"),
            deploys_dir=str(root / "source-deploys"),
            plans_dir=str(root / "source-plans"),
            sources_dir=str(root / "sources"),
        ),
    )


@pytest.fixture
def registry(tmp_path) -> SourceRegistry:
    SourceRegistry.reset_instance()
    directory = tmp_path / "sources"
    directory.mkdir()
    reg = SourceRegistry(sources_directory=directory, writable=True, refresh_interval=0)
    yield reg
    reg.close()
    SourceRegistry.reset_instance()


class TestSeeding:
    def test_the_core_marker_is_forced_not_read(self, registry, tmp_path):
        # A shipped file that omitted the marker would seed an operator-owned source
        # with every gate inactive, so the engine sets it rather than trusting it.
        root = tmp_path
        sources_dir = root / "schemas" / SOURCES_SUBDIR
        sources_dir.mkdir(parents=True)
        without_marker = {k: v for k, v in LANDING_DEFINITION.items() if k != "resource_type"}
        yaml_dump(without_marker, sources_dir / LANDING_SOURCE_FILE)

        seed_core_sources(registry=registry, settings=_settings(root=root, seeded=False))

        assert registry.get_source("main").resource_type == "core"

    def test_it_adopts_the_seeded_definition(self, registry, tmp_path):
        changed = seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        assert changed == ["main"]
        stored = registry.get_source("main")
        assert stored.resource_type == "core"
        assert stored.description == LANDING_DEFINITION["description"]

    def test_the_name_comes_from_the_landing_table_setting(self, registry, tmp_path):
        # The shipped file carries no `source`, so nothing is hard-coded to `main`.
        settings = _settings(root=tmp_path, table="catchall")

        seed_core_sources(registry=registry, settings=settings)

        stored = registry.get_source("catchall")
        assert stored.source == "catchall"
        assert stored.display_name == "catchall"
        assert stored.table_name == "catchall"

    def test_it_declares_no_origin(self, registry, tmp_path):
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        stored = registry.get_source("main")
        assert stored.match is None
        assert stored.fetcher is None
        assert stored.origin is None

    def test_its_version_is_already_deployed(self, registry, tmp_path):
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        stored = registry.get_source("main")
        assert stored.deployed_version == stored.current

    def test_seeding_twice_writes_nothing(self, registry, tmp_path):
        settings = _settings(root=tmp_path)
        seed_core_sources(registry=registry, settings=settings)

        assert seed_core_sources(registry=registry, settings=settings) == []

    def test_an_absent_definition_is_not_an_error(self, registry, tmp_path):
        # A deployment on an older dfe-schemas has no sources/ tree.
        changed = seed_core_sources(
            registry=registry, settings=_settings(root=tmp_path, seeded=False)
        )

        assert changed == []
        assert not (registry.source_exists("main"))

    def test_a_renamed_landing_table_replaces_the_stale_source(self, registry, tmp_path):
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        changed = seed_core_sources(
            registry=registry, settings=_settings(root=tmp_path, table="landing")
        )

        assert changed == ["main", "landing"]
        assert registry.source_exists("landing")
        assert not (registry.source_exists("main"))

    def test_it_records_the_source_as_built_and_deployed(self, registry, tmp_path):
        # The table exists once the bootstrap has run, so reporting the source as never
        # built and never deployed would be false.
        settings = _settings(root=tmp_path)

        seed_core_sources(registry=registry, settings=settings, tables_bootstrapped=True)

        source = registry.get_source("main")
        store = SourceDeploymentStore.from_settings(settings)
        build = store.load_build(source.source, source.current)
        deploy = store.load_deploy(source.source, source.current)

        assert build is not None
        assert build.column_count > 0
        assert build.create_table_ddl
        assert deploy is not None
        assert deploy.applied is True

    def test_the_recorded_ddl_is_the_one_the_bootstrap_applies(self, registry, tmp_path):
        # The source build path renders the landing table differently from the core
        # schema the bootstrap applies, so the record has to come from the bootstrap's.
        settings = _settings(root=tmp_path)

        seed_core_sources(registry=registry, settings=settings, tables_bootstrapped=True)

        source = registry.get_source("main")
        deploy = SourceDeploymentStore.from_settings(settings).load_deploy(
            source.source, source.current
        )

        assert deploy is not None
        assert deploy.create_table == landing_table_ddl(settings)

    def test_no_deploy_is_recorded_when_the_table_bootstrap_did_not_run(self, registry, tmp_path):
        # bootstrap_tables off, or an unreachable ClickHouse: there is no table to
        # have deployed to, so claiming one would be false.
        settings = _settings(root=tmp_path)

        seed_core_sources(registry=registry, settings=settings, tables_bootstrapped=False)

        source = registry.get_source("main")
        store = SourceDeploymentStore.from_settings(settings)

        assert store.load_build(source.source, source.current) is not None
        assert store.load_deploy(source.source, source.current) is None

    def test_a_rename_takes_the_stale_build_records_with_it(self, registry, tmp_path):
        # Left behind, they would make the next source of that name report itself
        # deployed before it ever was.
        seed_core_sources(
            registry=registry, settings=_settings(root=tmp_path), tables_bootstrapped=True
        )

        settings = _settings(root=tmp_path, table="landing")
        seed_core_sources(registry=registry, settings=settings, tables_bootstrapped=True)

        store = SourceDeploymentStore.from_settings(settings)
        current = LANDING_DEFINITION["current"]

        assert store.load_build("main", current) is None
        assert store.load_deploy("main", current) is None
        assert store.load_deploy("landing", current) is not None

    def test_a_changed_definition_replaces_the_stored_one(self, registry, tmp_path):
        # An engine upgrade re-seeds the schemas tree; a stored core source that never
        # followed would pin the deployment to the definition it first started on.
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))
        settings = _settings(root=tmp_path, seeded=False)
        updated = {**LANDING_DEFINITION, "description": "Reworded upstream"}
        yaml_dump(
            updated, Path(settings.schemas.schemas_dir) / SOURCES_SUBDIR / LANDING_SOURCE_FILE
        )

        changed = seed_core_sources(registry=registry, settings=settings)

        assert changed == ["main"]
        assert registry.get_source("main").description == "Reworded upstream"

    def test_it_leaves_an_operator_source_alone(self, registry, tmp_path):
        registry.create_source_from_write(
            {"source": "filebeat", "match": {"field": "_source", "value": "filebeat"}}
        )

        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        assert registry.source_exists("filebeat")


class TestCoreWriteGates:
    def test_saving_over_it_is_refused(self, registry, tmp_path):
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))
        stored = registry.get_source("main")

        with pytest.raises(SourceCoreResourceError, match="main"):
            registry.save_source(stored.model_copy(update={"description": "hijacked"}))

    def test_deleting_it_is_refused(self, registry, tmp_path):
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        with pytest.raises(SourceCoreResourceError, match="main"):
            registry.delete_source("main")

        assert registry.source_exists("main")

    def test_a_body_that_omits_resource_type_cannot_demote_it(self, registry, tmp_path):
        # The gate reads the STORED doc, so an overwrite claiming nothing is still refused.
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))

        with pytest.raises(SourceCoreResourceError):
            registry.save_source({"source": "main", "match": {"field": "_source", "value": "main"}})

        assert registry.get_source("main").resource_type == "core"

    def test_an_operator_source_is_still_writable(self, registry, tmp_path):
        seed_core_sources(registry=registry, settings=_settings(root=tmp_path))
        registry.create_source_from_write(
            {"source": "filebeat", "match": {"field": "_source", "value": "filebeat"}}
        )

        registry.delete_source("filebeat")

        assert not (registry.source_exists("filebeat"))


class TestShippedDefinition:
    def test_the_definition_dfe_schemas_ships_is_the_one_the_tests_use(self):
        # Guards against this suite drifting from the real file: if dfe-schemas changes
        # sources/main.yaml, the fixture above has to follow.
        import dfe_schemas

        shipped = Path(dfe_schemas.schemas_root()) / SOURCES_SUBDIR / LANDING_SOURCE_FILE
        if not (shipped.is_file()):
            pytest.skip("installed dfe-schemas ships no sources/ tree yet")

        from dfe_engine.yaml_utils import yaml_load

        assert yaml_load(shipped) == LANDING_DEFINITION
