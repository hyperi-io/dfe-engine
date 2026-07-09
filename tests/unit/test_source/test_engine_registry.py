"""Tests for the engine registry (engine_registry.py).

The registry gates the MergeTree-family VARIANT; a parameterised form is
accepted (the variant must be listed, the params are the caller's), and the
Replicated/Shared prefix - being topology-derived at DDL time - is not a
permitted variant.
"""

import pytest

from dfe_engine.source.engine_registry import EngineRegistry, InvalidEngineError


@pytest.fixture
def registry() -> EngineRegistry:
    return EngineRegistry.default()


class TestValidate:
    def test_bare_variant_ok(self, registry):
        registry.validate("MergeTree")
        registry.validate("ReplacingMergeTree")

    def test_parameterised_variant_ok(self, registry):
        # variant gates, params are the caller's
        registry.validate("ReplacingMergeTree(_timestamp_load)")
        registry.validate("SummingMergeTree(a, b)")

    def test_whitespace_before_paren_ok(self, registry):
        registry.validate("ReplacingMergeTree (ver)")

    def test_unknown_variant_rejected(self, registry):
        with pytest.raises(InvalidEngineError, match="Invalid engine"):
            registry.validate("Log")

    @pytest.mark.parametrize("engine", ["ReplicatedMergeTree", "SharedMergeTree"])
    def test_replicated_prefix_rejected(self, registry, engine):
        # The prefix is added at DDL time from the topology, never declared here.
        with pytest.raises(InvalidEngineError):
            registry.validate(engine)


class TestIntrospection:
    def test_engines_lists_the_family(self, registry):
        engines = registry.engines
        assert "MergeTree" in engines
        assert "ReplacingMergeTree" in engines
        # sorted for stable output
        assert engines == sorted(engines)

    def test_default_is_mergetree(self, registry):
        # plain MergeTree is always permitted (the safe baseline)
        registry.validate("MergeTree")
