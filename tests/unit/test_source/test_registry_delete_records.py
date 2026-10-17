#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_registry_delete_records.py
#  Purpose:      Deleting a source clears its build, plan and deploy records
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A source created under the name of a deleted one starts with no history.

The records sit in their own directories, outside the registry's, so deleting the
source definition alone leaves them for the next source of that name to read as its
own.
"""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from dfe_engine.source.deployment import (
    SchemaDeployResult,
    SourceBuildArtifact,
    SourceDeploymentStore,
    SourcePlanArtifact,
)
from dfe_engine.source.registry import SourceRegistry

NAME = "filebeat"
WRITE = {"source": NAME, "match": {"field": "_source", "value": NAME}}


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[SourceRegistry]:
    SourceRegistry.reset_instance()
    directory = tmp_path / "sources"
    directory.mkdir()
    reg = SourceRegistry(sources_directory=directory, writable=True, refresh_interval=0)
    yield reg
    reg.close()
    SourceRegistry.reset_instance()


@pytest.fixture
def store(tmp_path: Path) -> SourceDeploymentStore:
    return SourceDeploymentStore(
        builds_dir=tmp_path / "source-builds",
        plans_dir=tmp_path / "source-plans",
        deploys_dir=tmp_path / "source-deploys",
    )


def _record_history(registry: SourceRegistry, store: SourceDeploymentStore) -> str:
    """Give the stored source a build, a plan and an applied deploy, and return its version."""
    source = registry.get_source(NAME)
    version = source.current
    store.save_build(
        SourceBuildArtifact(
            source_name=NAME, version=version, built_at="2026-10-17T00:00:00Z", column_count=3
        ),
        source,
    )
    store.save_plan(
        SourcePlanArtifact(source_name=NAME, version=version, planned_at="2026-10-17T00:00:00Z"),
        source,
    )
    store.save_deploy(
        SchemaDeployResult(
            source_name=NAME,
            version=version,
            dry_run=False,
            applied=True,
            create_table="CREATE TABLE filebeat",
        ),
        source,
    )
    return version


def _history(store: SourceDeploymentStore, version: str) -> tuple[bool, bool, bool]:
    """Whether the build, the plan and the deploy of *version* are on disk."""
    return (
        store.load_build(NAME, version) is not None,
        store.load_plan(NAME, version) is not None,
        store.load_deploy(NAME, version) is not None,
    )


def test_a_recreated_source_starts_with_no_history(registry, store):
    registry.create_source_from_write(WRITE)
    version = _record_history(registry, store)
    assert _history(store, version) == (True, True, True)

    registry.delete_source(NAME, deployment_store=store)
    registry.create_source_from_write(WRITE)

    assert _history(store, version) == (False, False, False)
    assert store.load_deploy_document(NAME) is None
    assert registry.get_source(NAME).deployed_version is None


def test_another_sources_history_is_left_alone(registry, store):
    registry.create_source_from_write(WRITE)
    registry.create_source_from_write(
        {"source": "zeek", "match": {"field": "_source", "value": "zeek"}}
    )
    _record_history(registry, store)

    registry.delete_source("zeek", deployment_store=store)

    assert store.load_deploy_document(NAME) is not None


def test_the_default_store_is_the_one_the_loaded_settings_name(
    registry, store, tmp_path, monkeypatch
):
    registry.create_source_from_write(WRITE)
    version = _record_history(registry, store)
    loaded = SimpleNamespace(
        source=SimpleNamespace(
            sources_dir=str(tmp_path / "sources"),
            builds_dir=str(store.builds_dir),
            plans_dir=str(store.plans_dir),
            deploys_dir=str(store.deploys_dir),
        )
    )
    monkeypatch.setattr("dfe_engine.settings._settings", loaded)

    registry.delete_source(NAME)

    assert _history(store, version) == (False, False, False)


def test_a_store_that_cannot_clear_does_not_stop_the_delete(registry, store, audit_events):
    class _UnwritableStore(SourceDeploymentStore):
        def delete_source_records(self, source_name: str) -> list[Path]:
            raise PermissionError("source-deploys is read-only")

    registry.create_source_from_write(WRITE)
    version = _record_history(registry, store)
    unwritable = _UnwritableStore(
        builds_dir=store.builds_dir, plans_dir=store.plans_dir, deploys_dir=store.deploys_dir
    )

    registry.delete_source(NAME, deployment_store=unwritable)

    assert not registry.source_exists(NAME)
    assert _history(store, version) == (True, True, True)
    [warning] = [e for e in audit_events if "build records remain" in e["event"]]
    assert NAME in warning["event"]
    assert "read-only" in warning["event"]
