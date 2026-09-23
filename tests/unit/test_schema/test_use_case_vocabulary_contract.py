#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_use_case_vocabulary_contract.py
#  Purpose:      Pin the index use-case vocabulary between dfe-engine and dfe-schemas
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The index use-case vocabulary contract between dfe-engine and dfe-schemas.

dfe-schemas' ``registries/types.yaml`` owns the vocabulary. The engine emits a
use case into every schema it imports and renders an index from each one it
reads, and nothing asserted that the two sets agree -- so when dfe-schemas
retired ``fulltext`` the engine kept emitting it and the engine's own validator
rejected the result (dfe-engine#468).

The accepted set is read out of the registry YAML, never from an engine
constant: a test written against the engine's own list only checks the engine
against itself, which is how the rename survived.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sigma.collection import SigmaCollection

from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.schema.schema_loader import SchemaLoader, resolve_registry_path
from dfe_engine.services.schema import elastic_schema_service
from dfe_engine.services.schema.elastic_schema_service import _map_es_type
from dfe_engine.sigma.sigma_backend_clickhouse import (
    TEXT_INDEX_USE_CASES,
    SqlBackend,
    declares_text_index,
)
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import RETIRED_USE_CASES, TypeRegistry
from dfe_engine.yaml_utils import yaml_load

REGISTRY = yaml_load(resolve_registry_path("types.yaml"))
ACCEPTED: frozenset[str] = frozenset(REGISTRY["use_cases"])

# dfe-schemas registries/types.yaml, restated so a rename or removal there fails
# HERE, with the vocabulary named, instead of silently widening every assertion
# below. Same reason the loader's directive names are restated in
# test_loader_directive_contract.py.
EXPECTED_VOCABULARY = frozenset(
    {
        "dimension",
        "exact_match",
        "key_search",
        "range",
        "similarity_search",
        "substring_search",
        "word_search",
    }
)

# An ES type no match arm names, so the fallback arm is covered too.
UNKNOWN_ELASTIC_TYPE = "a_type_elasticsearch_does_not_have"


def _elastic_types_the_importer_handles() -> list[str]:
    """Every Elastic type named in ``_map_es_type``, read from its own match arms.

    Enumerating the arms beats listing them here: a new arm cannot be added
    without this contract covering what it emits.
    """
    tree = ast.parse(Path(elastic_schema_service.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_map_es_type"
    )
    named = [
        pattern.value.value
        for match in ast.walk(function)
        if isinstance(match, ast.Match)
        for case in match.cases
        for pattern in (
            case.pattern.patterns if isinstance(case.pattern, ast.MatchOr) else [case.pattern]
        )
        if isinstance(pattern, ast.MatchValue) and isinstance(pattern.value, ast.Constant)
    ]
    assert named, "no match arms found in _map_es_type -- the enumeration has gone stale"
    return named


ELASTIC_TYPES = [*_elastic_types_the_importer_handles(), UNKNOWN_ELASTIC_TYPE]


def _declared(use_case: str) -> str:
    """*use_case* as a column declares it, with the one argument the vocabulary takes."""
    return "similarity_search(768)" if use_case == "similarity_search" else use_case


@pytest.fixture(scope="module")
def registry() -> TypeRegistry:
    return TypeRegistry.default()


def test_the_registry_carries_the_vocabulary_this_contract_pins():
    """A use case added or renamed in dfe-schemas is a decision, not a silent widening."""
    assert ACCEPTED == EXPECTED_VOCABULARY


class TestWhatTheEngineEmits:
    @pytest.mark.parametrize("es_type", ELASTIC_TYPES)
    def test_an_imported_elastic_field_gets_a_use_case_the_registry_accepts(self, es_type: str):
        """An Elastic import that assigns a retired name cannot be saved back (#468)."""
        _, _, use_case = _map_es_type(es_type)

        assert use_case == "" or use_case in ACCEPTED, (
            f"the Elastic importer maps {es_type!r} to use case {use_case!r}, "
            f"which the registry rejects. Accepted: {sorted(ACCEPTED)}"
        )

    @pytest.mark.parametrize("es_type", ELASTIC_TYPES)
    def test_an_imported_elastic_field_validates_against_its_own_primitive(
        self, es_type: str, registry: TypeRegistry
    ):
        """A name in the vocabulary is still refused on the wrong primitive."""
        primitive, attributes, use_case = _map_es_type(es_type)
        column = SchemaColumn(
            name="c", type=primitive, attribute=list(attributes), use_case=use_case or None
        )

        assert SchemaLoader.validate_columns([column], registry) == []


class TestWhatTheEngineRenders:
    @pytest.mark.parametrize("use_case", sorted(EXPECTED_VOCABULARY))
    @pytest.mark.parametrize("legacy", [False, True], ids=["ga", "legacy"])
    def test_every_accepted_use_case_renders_an_index(
        self, use_case: str, legacy: bool, registry: TypeRegistry
    ):
        """A name the registry accepts and no template answers gets no index at all."""
        primitive = registry.valid_primitives_for_use_case(use_case)[0]
        generator = DDLGenerator(registry, use_legacy_indexes=legacy)
        column = SchemaColumn(name="c", type=primitive, use_case=_declared(use_case))

        assert generator._index_defs(column), f"{use_case} renders no index"

    def test_no_index_template_is_keyed_on_a_name_the_registry_rejects(self):
        """A template keyed on a retired name can never fire."""
        from dfe_engine.schema.schema_ddl import _INDEX_TEMPLATES, _INDEX_TEMPLATES_LEGACY

        keyed = set(_INDEX_TEMPLATES) | set(_INDEX_TEMPLATES_LEGACY)

        assert keyed <= ACCEPTED, f"dead index templates: {sorted(keyed - ACCEPTED)}"


class TestTheRetiredVocabulary:
    def test_no_retired_name_is_still_accepted(self):
        """Retired means the registry refuses it, which is what makes translation necessary."""
        assert not (set(RETIRED_USE_CASES) & ACCEPTED)

    @pytest.mark.parametrize(("retired", "current"), sorted(RETIRED_USE_CASES.items()))
    def test_a_retired_name_translates_to_one_the_registry_accepts(
        self, retired: str, current: str
    ):
        assert current in ACCEPTED

    @pytest.mark.parametrize("retired", sorted(RETIRED_USE_CASES))
    def test_a_column_stored_under_a_retired_name_validates(
        self, retired: str, registry: TypeRegistry
    ):
        """The #468 failure: promote-field validates the whole schema, and an
        untouched column stored before the rename refused the write."""
        primitive = registry.valid_primitives_for_use_case(RETIRED_USE_CASES[retired])[0]
        column = SchemaColumn.model_validate(
            {"name": "message", "type": primitive, "use_case": retired}
        )

        assert SchemaLoader.validate_columns([column], registry) == []

    @pytest.mark.parametrize(("retired", "current"), sorted(RETIRED_USE_CASES.items()))
    @pytest.mark.parametrize("legacy", [False, True], ids=["ga", "legacy"])
    def test_a_retired_name_renders_the_index_its_current_name_does(
        self, retired: str, current: str, legacy: bool, registry: TypeRegistry
    ):
        """Translation is a rename, so a table created before it keeps its index."""
        primitive = registry.valid_primitives_for_use_case(current)[0]
        generator = DDLGenerator(registry, use_legacy_indexes=legacy)
        stored = SchemaColumn.model_validate({"name": "c", "type": primitive, "use_case": retired})
        renamed = SchemaColumn.model_validate({"name": "c", "type": primitive, "use_case": current})

        assert generator._index_defs(stored) == generator._index_defs(renamed)

    @pytest.mark.parametrize("retired", sorted(RETIRED_USE_CASES))
    def test_a_stored_retired_name_reaches_the_ddl_as_a_real_index(
        self, retired: str, registry: TypeRegistry
    ):
        """End to end: a schema stored before the rename still creates an indexed table."""
        primitive = registry.valid_primitives_for_use_case(RETIRED_USE_CASES[retired])[0]
        column = SchemaColumn.model_validate(
            {"name": "message", "type": primitive, "use_case": retired}
        )
        ddl = DDLGenerator(registry).generate_create_table("events", [column], DDLConfig(db="dfe"))

        assert "INDEX idx_message" in ddl


def _indexed_expression(use_case: str, registry: TypeRegistry) -> str:
    """The expression the rendered INDEX line is built over, backticks stripped."""
    column = SchemaColumn(name="message", type="text", use_case=use_case)
    (line,) = DDLGenerator(registry)._index_defs(column)
    between_name_and_type = line.split("INDEX idx_message ", 1)[1].split(" TYPE ", 1)[0]
    return between_name_and_type.replace("`", "")


def _queried_expression(use_case: str) -> str:
    """The expression the Sigma backend filters on, read off the emitted predicate."""
    backend = SqlBackend(schema_metadata={"message": {"type": "text", "use_case": use_case}})
    rule = """
        title: Contract
        status: test
        logsource: {category: test, product: test}
        detection:
            selection:
                message|contains: 'needle'
            condition: selection
    """
    return backend.convert(SigmaCollection.from_yaml(rule))[0].split(" LIKE ", 1)[0]


class TestTheQuerySideReadsTheSameVocabulary:
    """The Sigma backend gates its text-index query form on the same vocabulary.

    It gated on the retired ``index_type`` / ``text_search`` pair instead, so the
    branch could never execute and no test saw it (dfe-engine#497).
    """

    def test_the_backend_keys_on_names_the_registry_accepts(self):
        """A gate keyed on a retired name can never fire."""
        assert TEXT_INDEX_USE_CASES <= ACCEPTED, (
            f"the Sigma backend keys its text-index path on "
            f"{sorted(TEXT_INDEX_USE_CASES - ACCEPTED)}, which the registry rejects"
        )

    @pytest.mark.parametrize("use_case", sorted(TEXT_INDEX_USE_CASES))
    def test_the_backend_keys_on_every_use_case_that_renders_a_text_index(
        self, use_case: str, registry: TypeRegistry
    ):
        """A use case rendering a text index the query side ignores buys storage for nothing."""
        (line,) = DDLGenerator(registry)._index_defs(
            SchemaColumn(name="message", type="text", use_case=use_case)
        )

        assert "TYPE text(" in line

    @pytest.mark.parametrize("retired", sorted(RETIRED_USE_CASES))
    def test_a_retired_name_reaches_the_text_index_query_path_iff_its_current_name_does(
        self, retired: str
    ):
        """Translation is one rename, so both spellings answer the same question."""
        current = RETIRED_USE_CASES[retired]

        assert declares_text_index({"use_case": retired}) == declares_text_index(
            {"use_case": current}
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the renderers index the bare column, so lower(col) LIKE prunes nothing yet: "
            "dfe-engine#496's schema half (schema_ddl._INDEX_TEMPLATES here and "
            "dfe-schemas render.py _INDEX_TEMPLATES) has still to index lower(col)"
        ),
    )
    @pytest.mark.parametrize("use_case", sorted(TEXT_INDEX_USE_CASES))
    def test_the_queried_expression_is_the_indexed_expression(
        self, use_case: str, registry: TypeRegistry
    ):
        """ClickHouse prunes only when the filter names the expression the index holds.

        Passing means the schema half landed and this xfail is the thing to delete.
        """
        assert _queried_expression(use_case) == _indexed_expression(use_case, registry)
