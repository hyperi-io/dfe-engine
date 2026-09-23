"""The e2e source seeder: create-once, but the display name still reconciles."""

from unittest.mock import Mock

import pytest

from dfe_engine.api.e2e.seed.sources import Sources
from dfe_engine.source.registry import SourceRegistry


@pytest.fixture
def registry(tmp_path) -> SourceRegistry:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    SourceRegistry.reset_instance()
    reg = SourceRegistry(sources_directory=sources_dir, writable=True, refresh_interval=0)
    yield reg
    reg.close()
    SourceRegistry.reset_instance()


@pytest.fixture
def seeder(registry) -> Sources:
    return Sources(
        account_store=Mock(),
        group_store=Mock(),
        org_registry=Mock(),
        env="test",
        source_registry=registry,
    )


def test_seed_source_is_create_once(seeder, registry):
    assert seeder.seed_source("firstrun") is True
    assert seeder.seed_source("firstrun") is False
    assert list(registry.get_source("firstrun").versions) == ["1.0.0"]


def test_a_renamed_fixture_reaches_an_existing_source(seeder, registry):
    """A changed display name applies to a source that already exists.

    Create-once left the stored name at whatever the first run wrote, so a
    fixture rename read as a dropped field and cost a UI-defect hunt (#489).
    """
    seeder.seed_transform_source("filebeatvrl", "vrl")
    assert registry.get_source("filebeatvrl").display_name == "Filebeat vrl"

    seeder._ensure_source("filebeatvrl", display_name="Filebeat VRL renamed")

    assert registry.get_source("filebeatvrl").display_name == "Filebeat VRL renamed"


def test_reconciling_the_display_name_bumps_no_version(seeder, registry):
    """``display_name`` sits beside ``versions``, so writing it adds none."""
    seeder.seed_source("stable")
    before = dict(registry.get_source("stable").versions)

    seeder._ensure_source("stable", display_name="A different label")

    after = registry.get_source("stable")
    assert list(after.versions) == list(before)
    assert after.display_name == "A different label"
