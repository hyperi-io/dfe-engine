"""Tests for the engine registry (engine_registry.py).

The registry gates the MergeTree-family VARIANT on every load, and each
variant's argument rule on save. The Replicated/Shared prefix is topology-derived
at DDL time, so it is not a permitted variant.
"""

import pytest

from dfe_engine.source.engine_registry import (
    EngineRegistry,
    EngineRegistryError,
    InvalidEngineError,
)


@pytest.fixture
def registry() -> EngineRegistry:
    return EngineRegistry.default()


class TestValidate:
    def test_bare_variant_ok(self, registry):
        registry.validate("MergeTree")
        registry.validate("ReplacingMergeTree")

    def test_parameterised_variant_ok(self, registry):
        registry.validate("ReplacingMergeTree(_timestamp_load)")
        registry.validate("SummingMergeTree(a, b)")

    def test_whitespace_before_paren_ok(self, registry):
        registry.validate("ReplacingMergeTree (ver)")

    def test_unknown_variant_rejected(self, registry):
        with pytest.raises(InvalidEngineError, match="Invalid engine"):
            registry.validate("Log")

    @pytest.mark.parametrize("engine", ["ReplicatedMergeTree", "SharedMergeTree"])
    def test_replicated_prefix_rejected(self, registry, engine):
        with pytest.raises(InvalidEngineError):
            registry.validate(engine)


class TestValidateArguments:
    @pytest.mark.parametrize(
        "engine",
        [
            "MergeTree",
            "MergeTree()",
            "ReplacingMergeTree",
            "ReplacingMergeTree(ver)",
            "CollapsingMergeTree(sign)",
            "VersionedCollapsingMergeTree(sign, ver)",
        ],
    )
    def test_engines_that_follow_their_rule_pass(self, registry, engine):
        registry.validate_arguments(engine)

    def test_arguments_on_a_variant_that_takes_none_are_refused(self, registry):
        with pytest.raises(InvalidEngineError, match="takes no arguments"):
            registry.validate_arguments("MergeTree(x)")

    def test_a_variant_that_requires_arguments_is_refused_without_them(self, registry):
        with pytest.raises(InvalidEngineError, match=r"requires arguments \(sign_column\)"):
            registry.validate_arguments("CollapsingMergeTree")

    def test_an_unknown_variant_is_refused(self, registry):
        with pytest.raises(InvalidEngineError, match="Invalid engine"):
            registry.validate_arguments("Log")

    @pytest.mark.parametrize(
        "engine",
        [
            "MergeTree() SETTINGS index_granularity=1",
            "ReplacingMergeTree(ver) ORDER BY x",
            "CollapsingMergeTree(sign",
            "SummingMergeTree(a) (b)",
            "SummingMergeTree(toString(a))",
        ],
    )
    def test_anything_beyond_one_argument_list_is_refused(self, engine, registry):
        with pytest.raises(InvalidEngineError, match="expected Variant or Variant"):
            registry.validate_arguments(engine)


class TestRegistryShape:
    def test_a_registry_without_engines_is_refused(self):
        with pytest.raises(EngineRegistryError, match="under 'engines'"):
            EngineRegistry({"engine": []})

    def test_an_entry_with_an_unknown_argument_rule_is_refused(self):
        with pytest.raises(EngineRegistryError, match="arguments must be one of"):
            EngineRegistry({"engines": [{"name": "MergeTree", "arguments": "sometimes"}]})

    def test_an_entry_without_a_name_is_refused(self):
        with pytest.raises(EngineRegistryError, match="has no name"):
            EngineRegistry({"engines": [{"arguments": "none"}]})


class TestIntrospection:
    def test_engines_lists_the_family_sorted(self, registry):
        engines = registry.engines
        assert "MergeTree" in engines
        assert "ReplacingMergeTree" in engines
        assert engines == sorted(engines)

    def test_options_carry_the_argument_rule_in_registry_order(self, registry):
        options = registry.options

        assert options[0].name == "MergeTree"
        assert options[0].arguments == "none"
        replacing = next(option for option in options if option.name == "ReplacingMergeTree")
        assert replacing.arguments == "optional"
        assert replacing.argument_hint
