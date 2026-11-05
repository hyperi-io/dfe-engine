"""The e2e source seeder: create-once, but the display name still reconciles."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest

from dfe_engine.api.e2e.seed.sources import Sources
from dfe_engine.source.registry import SourceNotFoundError, SourceRegistry


@pytest.fixture
def registry(tmp_path) -> Iterator[SourceRegistry]:
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


def test_searchable_sources_stay_out_of_the_pipeline(seeder, registry):
    """Fifty list rows, none deployed, so a second call writes nothing and routing is untouched."""
    from dfe_engine.api.e2e.seed.sources import searchable_source_names

    assert seeder.seed_searchable_sources() is True
    assert seeder.seed_searchable_sources() is False
    names = searchable_source_names()
    assert len(names) == 50
    for num, name in enumerate(names):
        source = registry.get_source(name)
        assert source.display_name == f"Searchable Source {num}"
        assert source.description == f"Searchable source {num}."
        assert source.deployed_version is None
        assert source.transform is None
        assert source.fetcher is None

    seeder.delete_all()
    for name in names:
        with pytest.raises(SourceNotFoundError):
            registry.get_source(name)


def test_searchable_source_columns_come_from_one_deployed_source(seeder, registry):
    """One deployed source naming a meta schema, so its columns page has rows."""
    from dfe_engine.api.e2e.seed.sources import SEARCHABLE_COLUMNS_SOURCE

    assert seeder.seed_searchable_source_columns() is True
    assert seeder.seed_searchable_source_columns() is False
    source = registry.get_source(SEARCHABLE_COLUMNS_SOURCE)
    assert source.display_name == "Searchable Columns"
    assert source.deployed_version == source.current
    assert source.version().effective_schema().meta_schema == "meta/syslog"

    seeder.delete_all()
    with pytest.raises(SourceNotFoundError):
        registry.get_source(SEARCHABLE_COLUMNS_SOURCE)


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


@pytest.mark.parametrize(
    ("script", "name", "engine"),
    [
        ("seed_source", "firstrun", "vrl"),
        ("seed_transform_source", "filebeatvector", "vector"),
        ("seed_transform_source", "filebeatelastic", "elastic"),
    ],
)
def test_a_seeded_source_is_deployed_and_names_its_transform(
    seeder, registry, script, name, engine
):
    """A transform instance exists only for a deployed source that names one, so the seed does both."""
    if script == "seed_source":
        seeder.seed_source(name)
    else:
        seeder.seed_transform_source(name, engine)

    source = registry.get_source(name)
    assert source.deployed_version == source.current
    assert source.transform is not None
    assert source.transform.engine == engine


def test_a_seeded_source_gets_its_topics_where_the_deployment_manages_them(registry, monkeypatch):
    """The seeded transform reads the source's land topic, so the seed creates it as a deploy does."""
    from dfe_engine.kafka import topics

    asked: list[tuple[str, list]] = []
    monkeypatch.setattr(topics, "topics_managed", lambda _settings: True)
    monkeypatch.setattr(topics, "build_admin", lambda **_kw: "admin")
    monkeypatch.setattr(topics, "source_replication_factor", lambda *_a, **_kw: 1)
    monkeypatch.setattr(topics, "deployment_topic_config", lambda _settings: {})
    monkeypatch.setattr(
        topics,
        "source_topic_specs",
        lambda source, **_kw: [f"{source.source}_land", f"{source.source}_load"],
    )
    monkeypatch.setattr(
        topics,
        "ensure_topics",
        lambda specs, **_kw: asked.append(("ensure", list(specs))) or Mock(failed=[]),
    )
    settings = Mock()
    settings.kafka.topic_partitions = 1
    seeder = Sources(
        account_store=Mock(),
        group_store=Mock(),
        org_registry=Mock(),
        env="test",
        source_registry=registry,
        settings=settings,
    )

    seeder.seed_source("busrun")

    assert asked == [("ensure", ["busrun_land", "busrun_load"])]


def test_an_unreachable_broker_does_not_fail_the_seed(registry, monkeypatch):
    from dfe_engine.kafka import topics

    def unreachable(**_kw):
        raise RuntimeError("no broker")

    monkeypatch.setattr(topics, "topics_managed", lambda _settings: True)
    monkeypatch.setattr(topics, "build_admin", unreachable)
    seeder = Sources(
        account_store=Mock(),
        group_store=Mock(),
        org_registry=Mock(),
        env="test",
        source_registry=registry,
        settings=Mock(),
    )

    assert seeder.seed_source("nobroker") is True
