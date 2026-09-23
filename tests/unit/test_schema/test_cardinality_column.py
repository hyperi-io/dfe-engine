#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_cardinality_column.py
#  Purpose:      One declared cardinality decides the wrapper and the index
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The engine renders the same DDL dfe-schemas does, off the same declaration.

Cardinality used to have two homes: ``attribute: [lowcardinality]`` set the
storage wrapper by hand, and ``exact_match`` chose ``set(0)`` over
``bloom_filter`` separately. Both answer one question, and nothing kept them
agreeing. These tests pin the single answer on the engine's side of the format,
including that ``unknown`` costs no dictionary and falls to the bloom filter.

The rendered strings were accepted by a live ClickHouse 26.3.32.14 server.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dfe_engine.schema.models import SchemaColumn as MetaSchemaColumn
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import (
    CARDINALITIES,
    InvalidCardinalityError,
    TypeRegistry,
    fold_cardinality,
)


@pytest.fixture(scope="module")
def registry():
    return TypeRegistry.default()


@pytest.fixture(scope="module")
def generator(registry):
    return DDLGenerator(registry)


def column(**kwargs) -> SchemaColumn:
    return SchemaColumn.model_validate({"name": "c", "type": "string", **kwargs})


# -- the declaration --------------------------------------------------------


class TestWhatAColumnDeclares:
    @pytest.mark.parametrize("declared", CARDINALITIES)
    def test_a_column_carries_the_cardinality_it_declares(self, declared):
        assert column(cardinality=declared).declared_cardinality == declared

    def test_a_column_declaring_nothing_is_unknown(self):
        assert column().declared_cardinality == "unknown"

    def test_the_retired_attribute_still_reads_as_low(self):
        assert column(attribute=["lowcardinality"]).declared_cardinality == "low"

    def test_a_declared_cardinality_beats_the_attribute_it_agrees_with(self):
        assert column(cardinality="low", attribute=["lowcardinality"]).cardinality == "low"

    def test_declaring_the_attribute_against_the_cardinality_is_refused(self):
        """The disagreement between the two homes is the defect this removes."""
        with pytest.raises(ValidationError, match="contradicts"):
            column(cardinality="high", attribute=["lowcardinality"])

    def test_the_meta_schema_column_refuses_it_too(self):
        with pytest.raises(ValidationError, match="contradicts"):
            MetaSchemaColumn.model_validate(
                {
                    "name": "c",
                    "type": "string",
                    "cardinality": "high",
                    "attribute": ["lowcardinality"],
                }
            )

    def test_a_cardinality_the_vocabulary_does_not_carry_is_refused(self):
        with pytest.raises(ValidationError):
            column(cardinality="medium")

    def test_the_registry_refuses_it_by_name(self, registry):
        with pytest.raises(InvalidCardinalityError, match="Unknown cardinality"):
            registry.validate_cardinality("medium")

    def test_the_registry_declares_the_vocabulary_the_engine_uses(self, registry):
        """dfe-schemas registries/types.yaml is the SSoT, not a constant here."""
        assert tuple(registry.cardinalities) == CARDINALITIES

    @pytest.mark.parametrize(
        ("cardinality", "attributes", "expected"),
        [
            (None, None, "unknown"),
            (None, [], "unknown"),
            (None, ["lowcardinality"], "low"),
            (None, ["not_null"], "unknown"),
            ("high", ["not_null"], "high"),
            ("low", None, "low"),
        ],
    )
    def test_the_fold_is_one_rule(self, cardinality, attributes, expected):
        assert fold_cardinality(cardinality, attributes) == expected


# -- the storage decision ---------------------------------------------------


class TestTheStorageWrapper:
    @pytest.mark.parametrize(
        ("declared", "expected"),
        [
            ("low", "LowCardinality(Nullable(String))"),
            ("high", "Nullable(String)"),
            ("unknown", "Nullable(String)"),
        ],
    )
    def test_only_low_wraps_the_column(self, registry, declared, expected):
        assert registry.resolve("string", cardinality=declared).ch_type == expected

    def test_a_column_declaring_nothing_gets_no_wrapper(self, registry):
        assert registry.resolve("string").ch_type == "Nullable(String)"

    def test_the_retired_attribute_still_wraps(self, registry):
        resolved = registry.resolve("string", attributes=["lowcardinality"])
        assert resolved.ch_type == "LowCardinality(Nullable(String))"

    def test_nullable_stays_inside_the_wrapper(self, registry):
        """ClickHouse's order: LowCardinality(Nullable(T)), never the other way."""
        resolved = registry.resolve("string", cardinality="low", attributes=["not_null"])
        assert resolved.ch_type == "LowCardinality(String)"

    def test_low_wraps_an_exact_clickhouse_type_too(self, registry):
        resolved = registry.resolve("string", cardinality="low", ch_override="String")
        assert resolved.ch_type == "LowCardinality(String)"


# -- the index decision -----------------------------------------------------


class TestTheExactMatchIndex:
    @pytest.mark.parametrize(
        ("declared", "expected"),
        [
            ("low", "INDEX idx_c `c` TYPE set(0) GRANULARITY 4"),
            ("high", "INDEX idx_c `c` TYPE bloom_filter GRANULARITY 4"),
            ("unknown", "INDEX idx_c `c` TYPE bloom_filter GRANULARITY 4"),
        ],
    )
    def test_exact_match_reads_the_same_declaration(self, generator, declared, expected):
        col = column(use_case="exact_match", cardinality=declared)
        assert generator._index_defs(col) == [expected]

    def test_a_column_declaring_nothing_falls_to_the_bloom_filter(self, generator):
        """The safe way to be wrong: no dictionary, and a bounded index."""
        col = column(use_case="exact_match")
        assert generator._index_defs(col) == ["INDEX idx_c `c` TYPE bloom_filter GRANULARITY 4"]

    def test_the_retired_attribute_still_gets_the_exact_index(self, generator):
        col = column(use_case="exact_match", attribute=["lowcardinality"])
        assert generator._index_defs(col) == ["INDEX idx_c `c` TYPE set(0) GRANULARITY 4"]


# -- the two decisions, rendered together -----------------------------------


@pytest.fixture(scope="module")
def rendered(generator):
    """One CREATE TABLE carrying a low, a high and an undeclared column."""
    columns = [
        column(name="bounded", cardinality="low", use_case="exact_match"),
        column(name="unbounded", cardinality="high", use_case="exact_match"),
        column(name="unmeasured", use_case="exact_match"),
    ]
    return generator.generate_create_table("proof", columns, DDLConfig(db="dfe"))


def line_for(statement: str, name: str, needle: str) -> str:
    lines = (line.strip() for line in statement.splitlines())
    return next(line for line in lines if line.startswith(needle.format(name=name)))


class TestTheRenderedTable:
    def test_only_the_low_column_is_wrapped(self, rendered):
        assert "LowCardinality(Nullable(String))" in line_for(rendered, "bounded", "`{name}`")
        assert "LowCardinality" not in line_for(rendered, "unbounded", "`{name}`")
        assert "LowCardinality" not in line_for(rendered, "unmeasured", "`{name}`")

    def test_only_the_low_column_is_indexed_exactly(self, rendered):
        assert "TYPE set(0)" in line_for(rendered, "bounded", "INDEX idx_{name} ")
        assert "TYPE bloom_filter" in line_for(rendered, "unbounded", "INDEX idx_{name} ")
        assert "TYPE bloom_filter" in line_for(rendered, "unmeasured", "INDEX idx_{name} ")


# -- the field survives a round trip ----------------------------------------


class TestPersistence:
    def test_a_declared_cardinality_is_written_back(self):
        col = MetaSchemaColumn.model_validate(
            {"name": "c", "type": "string", "cardinality": "high"}
        )
        assert col.to_yaml_dict()["cardinality"] == "high"

    def test_a_column_that_declares_none_stays_out_of_the_yaml(self):
        """Absent means unknown, so a rewrite does not churn 26 shipped schemas."""
        col = MetaSchemaColumn.model_validate({"name": "c", "type": "string"})
        assert "cardinality" not in col.to_yaml_dict()

    def test_the_retired_attribute_is_written_back_unchanged(self):
        col = MetaSchemaColumn.model_validate(
            {"name": "c", "type": "string", "attribute": ["lowcardinality"]}
        )
        written = col.to_yaml_dict()
        assert written["attribute"] == ["lowcardinality"]
        assert "cardinality" not in written


# -- validation against the registry ----------------------------------------


class TestRegistryValidation:
    def test_a_valid_cardinality_passes(self, registry):
        assert column(cardinality="high").validate_against_registry(registry) == []

    def test_a_column_declaring_none_passes(self, registry):
        assert column().validate_against_registry(registry) == []
