"""Tests for source routing config generation."""

from __future__ import annotations

import pytest

from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    ReceiverRoutingConfig,
    SourceMatchRule,
)
from dfe_engine.services.source_routing import (
    compile_loader_routing,
    compile_receiver_routing,
)
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    name: str,
    *,
    match_field: str | None = None,
    match_value: str | None = None,
    enabled: bool = True,
) -> Source:
    data: dict = {"source": name, "enabled": enabled}
    if match_field and match_value:
        data["match"] = {"field": match_field, "value": match_value}
    return Source.model_validate(data)


class FakeSourceRegistry:
    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.source: s for s in sources}

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source '{source_name}' not found")
        return self._sources[source_name]

    def get_all_sources(self, enabled_only: bool = False) -> list[Source]:
        sources = list(self._sources.values())
        if enabled_only:
            sources = [s for s in sources if s.enabled]
        return sources


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sources():
    return [
        _make_source("filebeat", match_field="agent.type", match_value="filebeat"),
        _make_source("syslog", match_field="tags.event.category", match_value="syslog"),
        _make_source("crowdstrike_edr"),  # No match — SaaS fetcher source
        _make_source("disabled_src", match_field="x", match_value="y", enabled=False),
    ]


@pytest.fixture
def registry(sources):
    return FakeSourceRegistry(sources)


# ---------------------------------------------------------------------------
# Tests: SourceMatchRule model
# ---------------------------------------------------------------------------


class TestSourceMatchRuleModel:
    def test_create(self):
        rule = SourceMatchRule(field="agent.type", value="filebeat", topic="filebeat_land")
        assert rule.field == "agent.type"
        assert rule.value == "filebeat"
        assert rule.topic == "filebeat_land"

    def test_serialization(self):
        rule = SourceMatchRule(field="x", value="y", topic="z_land")
        d = rule.model_dump()
        assert d == {"field": "x", "value": "y", "topic": "z_land"}


# ---------------------------------------------------------------------------
# Tests: ReceiverRoutingConfig source_routing fields
# ---------------------------------------------------------------------------


class TestReceiverRoutingSourceRouting:
    def test_defaults_source_routing_false(self):
        config = ReceiverRoutingConfig()
        assert config.source_routing is False
        assert config.source_match_table == []

    def test_source_routing_with_match_table(self):
        rules = [
            SourceMatchRule(field="agent.type", value="filebeat", topic="filebeat_land"),
        ]
        config = ReceiverRoutingConfig(source_routing=True, source_match_table=rules)
        assert config.source_routing is True
        assert len(config.source_match_table) == 1


# ---------------------------------------------------------------------------
# Tests: LoaderRoutingConfig source_routing fields
# ---------------------------------------------------------------------------


class TestLoaderRoutingSourceRouting:
    def test_defaults_source_routing_false(self):
        config = LoaderRoutingConfig()
        assert config.source_routing is False
        assert config.source_field == "_source"

    def test_source_routing_enabled(self):
        config = LoaderRoutingConfig(source_routing=True, source_field="_source")
        assert config.source_routing is True
        assert config.source_field == "_source"


# ---------------------------------------------------------------------------
# Tests: compile_receiver_routing
# ---------------------------------------------------------------------------


class TestCompileReceiverRouting:
    def test_compiles_match_table(self, registry):
        config = compile_receiver_routing(registry)
        assert config.source_routing is True
        assert config.default_topic == "unmatched"

        # Only filebeat and syslog have match rules (disabled_src excluded)
        assert len(config.source_match_table) == 2
        topics = {r.topic for r in config.source_match_table}
        assert "filebeat_land" in topics
        assert "syslog_land" in topics

    def test_excludes_sources_without_match(self, registry):
        config = compile_receiver_routing(registry)
        topics = {r.topic for r in config.source_match_table}
        # crowdstrike_edr has no match rule
        assert "crowdstrike_edr_land" not in topics

    def test_excludes_disabled_sources(self, registry):
        config = compile_receiver_routing(registry)
        topics = {r.topic for r in config.source_match_table}
        assert "disabled_src_land" not in topics

    def test_custom_default_topic(self, registry):
        config = compile_receiver_routing(registry, default_topic="dlq")
        assert config.default_topic == "dlq"

    def test_match_fields_populated(self, registry):
        config = compile_receiver_routing(registry)
        rules_by_topic = {r.topic: r for r in config.source_match_table}

        fb = rules_by_topic["filebeat_land"]
        assert fb.field == "agent.type"
        assert fb.value == "filebeat"

        sl = rules_by_topic["syslog_land"]
        assert sl.field == "tags.event.category"
        assert sl.value == "syslog"

    def test_empty_registry(self):
        empty = FakeSourceRegistry([])
        config = compile_receiver_routing(empty)
        assert config.source_routing is True
        assert config.source_match_table == []


# ---------------------------------------------------------------------------
# Tests: compile_loader_routing
# ---------------------------------------------------------------------------


class TestCompileLoaderRouting:
    def test_compiles_source_routing(self, registry):
        config = compile_loader_routing(registry)
        assert config.source_routing is True
        assert config.source_field == "_source"
        assert config.default_db == "common"

    def test_category_to_table_populated(self, registry):
        config = compile_loader_routing(registry)
        # All enabled sources should appear
        assert "filebeat" in config.category_to_table
        assert "syslog" in config.category_to_table
        assert "crowdstrike_edr" in config.category_to_table
        # Disabled excluded
        assert "disabled_src" not in config.category_to_table

    def test_custom_db(self, registry):
        config = compile_loader_routing(registry, db="prod")
        assert config.default_db == "prod"

    def test_custom_source_field(self, registry):
        config = compile_loader_routing(registry, source_field="source_name")
        assert config.source_field == "source_name"

    def test_empty_registry(self):
        empty = FakeSourceRegistry([])
        config = compile_loader_routing(empty)
        assert config.source_routing is True
        assert config.category_to_table == {}
