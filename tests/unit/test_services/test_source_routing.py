"""Tests for source routing config generation.

The receiver emit targets the REAL dfe-receiver ``routing`` serde contract
(src/config/mod.rs SourceRule/RoutingConfig) - the round-trip test below pins
the exact field names.
"""

from __future__ import annotations

import pytest

from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import (
    ReceiverRoutingConfig,
    SourceRule,
)
from dfe_engine.services.source_routing import (
    UnsupportedMatchOperatorError,
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
    match_operator: str = "equals",
    state: str = "active",
) -> Source:
    data: dict = {"source": name, "state": state}
    if match_field:
        data["match"] = {
            "field": match_field,
            "operator": match_operator,
            "value": match_value or "",
        }
    return Source.model_validate(data)


class FakeSourceRegistry:
    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.source: s for s in sources}

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source '{source_name}' not found")
        return self._sources[source_name]

    def get_all_sources(
        self, enabled_only: bool = False, *, states: tuple[str, ...] | None = None
    ) -> list[Source]:
        sources = list(self._sources.values())
        if states is not None:
            sources = [s for s in sources if s.state in states]
        elif enabled_only:
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
        _make_source(
            "crowdstrike_edr", match_field="_source_fetcher", match_value="crowdstrike"
        ),  # SaaS fetcher source — still carries a match
        _make_source("present_src", match_field="tags.present_marker", match_operator="exists"),
        _make_source("disabled_src", match_field="x", match_value="y", state="disabled"),
        _make_source("dormant_src", match_field="d", match_value="d", state="dormant"),
    ]


@pytest.fixture
def registry(sources):
    return FakeSourceRegistry(sources)


# ---------------------------------------------------------------------------
# Tests: SourceRule model (receiver serde contract)
# ---------------------------------------------------------------------------


class TestSourceRuleModel:
    def test_create(self):
        rule = SourceRule(
            field="agent.type", mode="key_value_set", match_value="filebeat", source="filebeat"
        )
        assert rule.field == "agent.type"
        assert rule.mode == "key_value_set"
        assert rule.match_value == "filebeat"
        assert rule.source == "filebeat"

    def test_serialization_matches_receiver_serde(self):
        rule = SourceRule(field="x", mode="key_value_set", match_value="y", source="z")
        d = rule.model_dump()
        # EXACT receiver field names (dfe-receiver src/config/mod.rs SourceRule)
        assert d == {"field": "x", "mode": "key_value_set", "match_value": "y", "source": "z"}

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            SourceRule(field="x", mode="regex_match", source="z")


class TestReceiverRoutingConfigModel:
    def test_defaults_match_receiver_defaults(self):
        config = ReceiverRoutingConfig()
        assert config.source_rules == []
        assert config.default_source == "default"
        assert config.topic_suffix == "_land"
        assert config.source_to_topic == {}
        assert config.legacy_compat is False


# ---------------------------------------------------------------------------
# Tests: compile_receiver_routing
# ---------------------------------------------------------------------------


class TestCompileReceiverRouting:
    def test_compiles_source_rules(self, registry):
        config = compile_receiver_routing(registry)
        assert config.default_source == "default"
        assert config.topic_suffix == "_land"

        # active sources with a match (disabled + dormant excluded)
        by_source = {r.source: r for r in config.source_rules}
        assert set(by_source) == {"filebeat", "syslog", "crowdstrike_edr", "present_src"}

    def test_equals_maps_to_key_value_set(self, registry):
        config = compile_receiver_routing(registry)
        fb = next(r for r in config.source_rules if r.source == "filebeat")
        assert fb.mode == "key_value_set"
        assert fb.field == "agent.type"
        assert fb.match_value == "filebeat"

    def test_exists_maps_to_key_present(self, registry):
        config = compile_receiver_routing(registry)
        pr = next(r for r in config.source_rules if r.source == "present_src")
        assert pr.mode == "key_present"
        assert pr.field == "tags.present_marker"
        assert pr.match_value is None

    def test_excludes_disabled_and_dormant_sources(self, registry):
        config = compile_receiver_routing(registry)
        stamped = {r.source for r in config.source_rules}
        assert "disabled_src" not in stamped
        assert "dormant_src" not in stamped

    def test_no_topic_overrides_for_default_derivation(self, registry):
        # topic_land == f"{source}{topic_suffix}" for every source, so no
        # source_to_topic entries are emitted.
        config = compile_receiver_routing(registry)
        assert config.source_to_topic == {}

    def test_unsupported_operator_rejected(self):
        reg = FakeSourceRegistry(
            [_make_source("bad", match_field="f", match_value="v", match_operator="includes")]
        )
        with pytest.raises(UnsupportedMatchOperatorError, match="documented receiver gap"):
            compile_receiver_routing(reg)

    def test_empty_registry(self):
        empty = FakeSourceRegistry([])
        config = compile_receiver_routing(empty)
        assert config.source_rules == []

    def test_round_trip_against_receiver_serde_shape(self, registry):
        """The emitted YAML/JSON round-trips against the receiver's serde shape.

        Pins the exact key set the Rust receiver deserialises:
        routing.source_rules[].{field,mode,match_value,source} +
        default_source + topic_suffix + source_to_topic.
        """
        from dfe_engine.yaml_utils import yaml_dump_string, yaml_load_string

        config = compile_receiver_routing(registry)
        emitted = yaml_load_string(yaml_dump_string(config.model_dump(mode="json")))

        assert set(emitted) == {
            "source_rules",
            "default_source",
            "topic_suffix",
            "source_to_topic",
            "legacy_compat",
            "dlq",
        }
        for rule in emitted["source_rules"]:
            assert set(rule) == {"field", "mode", "match_value", "source"}
            assert rule["mode"] in ("key_present", "key_value_set", "key_value_use")

        # the engine model itself re-validates the emitted doc (serde-compatible)
        assert ReceiverRoutingConfig.model_validate(emitted).default_source == "default"


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
        # All ACTIVE sources should appear
        assert "filebeat" in config.category_to_table
        assert "syslog" in config.category_to_table
        assert "crowdstrike_edr" in config.category_to_table
        # Disabled + dormant excluded
        assert "disabled_src" not in config.category_to_table
        assert "dormant_src" not in config.category_to_table

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


class TestLoaderRoutingSourceRouting:
    def test_defaults_source_routing_false(self):
        config = LoaderRoutingConfig()
        assert config.source_routing is False
        assert config.source_field == "_source"
