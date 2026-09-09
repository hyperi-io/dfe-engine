"""Tests for the engine resolver (engine_resolver.py).

Covers the pure surface: parse_engine (variant/param split) and the no-client
cascade (override -> setting -> default) plus clause rendering per topology.
Live sensing needs a real ClickHouse server and is exercised in the integration
suite, not mocked here (no-mocks policy).
"""

import pytest

from dfe_engine.schema.engine_resolver import (
    EngineResolver,
    EngineSpec,
    Topology,
    parse_engine,
)
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

# ── parse_engine ────────────────────────────────────────────────────


class TestParseEngine:
    def test_bare_variant(self):
        spec = parse_engine("MergeTree")
        assert spec == EngineSpec(variant="MergeTree", params="")

    def test_parameterised(self):
        spec = parse_engine("ReplacingMergeTree(last_fired_at)")
        assert spec == EngineSpec(variant="ReplacingMergeTree", params="last_fired_at")

    def test_multi_param(self):
        spec = parse_engine("SummingMergeTree(a, b)")
        assert spec == EngineSpec(variant="SummingMergeTree", params="a, b")

    def test_empty_parens_is_no_params(self):
        spec = parse_engine("MergeTree()")
        assert spec == EngineSpec(variant="MergeTree", params="")

    def test_whitespace_tolerant(self):
        spec = parse_engine("  ReplacingMergeTree( ver ) ")
        assert spec == EngineSpec(variant="ReplacingMergeTree", params="ver")


# ── clause rendering per topology (via override) ────────────────────


class TestResolveClause:
    def test_single_plain(self):
        r = EngineResolver(override="single").resolve(parse_engine("MergeTree"), "db")
        assert r.clause == "MergeTree()"
        assert r.on_cluster == ""
        assert r.topology == "single"
        assert r.origin == "config-override"

    def test_single_parameterised(self):
        r = EngineResolver(override="single").resolve(parse_engine("ReplacingMergeTree(ver)"), "db")
        assert r.clause == "ReplacingMergeTree(ver)"

    def test_replicated_bare(self):
        r = EngineResolver(override="replicated").resolve(parse_engine("MergeTree"), "db")
        # argumentless - server supplies path/replica via macros
        assert r.clause == "ReplicatedMergeTree"
        assert r.topology == "replicated"

    def test_replicated_parameterised(self):
        r = EngineResolver(override="replicated").resolve(
            parse_engine("ReplacingMergeTree(ver)"), "db"
        )
        assert r.clause == "ReplicatedReplacingMergeTree(ver)"

    def test_override_carries_no_on_cluster(self):
        # A named topology override never emits ON CLUSTER - that only comes from
        # sensing an Atomic cluster (which needs a live client).
        r = EngineResolver(override="replicated").resolve(parse_engine("MergeTree"), "db")
        assert r.on_cluster == ""


class TestBuilderCarriesTheResolver:
    def test_builder_ddl_follows_an_injected_resolver(self):
        from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2

        columns = [SchemaColumn(name="_timestamp", type="timestamp")]
        replicated = SchemaBuilderV2(resolver=EngineResolver(override="replicated"))
        assert "ENGINE = ReplicatedMergeTree" in replicated.build_ddl_only(columns, "t")
        plain = SchemaBuilderV2()
        assert "ENGINE = MergeTree()" in plain.build_ddl_only(columns, "t")


# ── cascade (no client) ─────────────────────────────────────────────


class TestCascade:
    def test_override_wins_over_setting(self):
        r = EngineResolver(override="single", topology_setting="replicated").resolve(
            parse_engine("MergeTree"), "db"
        )
        assert r.topology == "single"
        assert r.origin == "config-override"

    def test_setting_used_without_override(self):
        r = EngineResolver(topology_setting="replicated").resolve(parse_engine("MergeTree"), "db")
        assert r.topology == "replicated"
        assert r.origin == "topology-setting"

    def test_terminal_default_is_single(self):
        r = EngineResolver().resolve(parse_engine("MergeTree"), "db")
        assert r.topology == "single"
        assert r.origin == "default"

    @pytest.mark.parametrize("name", ["single", "SINGLE", " Single "])
    def test_single_name_case_insensitive(self, name):
        r = EngineResolver(override=name).resolve(parse_engine("MergeTree"), "db")
        assert r.topology == "single"

    @pytest.mark.parametrize("name", ["replicated", "cluster", "anything_else"])
    def test_non_single_name_is_replicated(self, name):
        r = EngineResolver(override=name).resolve(parse_engine("MergeTree"), "db")
        assert r.topology == "replicated"


def test_topology_enum_values():
    assert Topology.SINGLE.value == "single"
    assert Topology.REPLICATED.value == "replicated"


# ── generator wiring ────────────────────────────────────────────────
# The generator must defer to an injected resolver. Live paths inject one built
# with a client so the engine is SENSED; if the generator quietly kept resolving
# from DDLConfig.topology instead, every table would fall back to the "single"
# default and a clustered deployment would silently create unreplicated tables
# per-node (the 2026-07-16 dfe-k8s split-brain).


class TestGeneratorHonoursInjectedResolver:
    def _columns(self):
        return [SchemaColumn(name="_timestamp", type="timestamp")]

    def test_injected_resolver_overrides_config_topology(self):
        gen = DDLGenerator(
            TypeRegistry.default(),
            resolver=EngineResolver(override="replicated"),
        )
        # cfg.topology is the "single" default and must NOT win.
        ddl = gen.generate_create_table("t", self._columns(), DDLConfig(db="d"))
        assert "ENGINE = ReplicatedMergeTree" in ddl

    def test_without_resolver_config_topology_still_applies(self):
        gen = DDLGenerator(TypeRegistry.default())
        ddl = gen.generate_create_table(
            "t", self._columns(), DDLConfig(db="d", topology="replicated")
        )
        assert "ENGINE = ReplicatedMergeTree" in ddl

    def test_default_stays_plain_mergetree(self):
        gen = DDLGenerator(TypeRegistry.default())
        ddl = gen.generate_create_table("t", self._columns(), DDLConfig(db="d"))
        assert "ENGINE = MergeTree()" in ddl
        assert "ON CLUSTER" not in ddl

    def test_explicit_cluster_pin_still_emits_on_cluster(self):
        gen = DDLGenerator(TypeRegistry.default())
        ddl = gen.generate_create_table(
            "t", self._columns(), DDLConfig(db="d", cluster="mycluster")
        )
        assert "ON CLUSTER mycluster" in ddl
