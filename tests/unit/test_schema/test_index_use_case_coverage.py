#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_index_use_case_coverage.py
#  Purpose:      Every DDL catalogue use case has a row in the live index pruning table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A use case cannot land in the dfe-schemas DDL catalogue without a live pruning row.

The pruning test runs only in the integration tier, so this check runs in every tier
and fails the moment a new catalogue entry, or a cardinality that renders a different
index, has no row in ``tests/support/index_use_cases.py``.
"""

from pathlib import Path

import yaml
from dfe_schemas.ddl_catalogue import DDLCatalogue, IndexSpec
from dfe_schemas.loader import CARDINALITIES, DEFAULT_CARDINALITY, TypeRegistry
from dfe_schemas.render import split_use_case

from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.source.models import Source
from tests.support.index_use_cases import CASES, columns, meta_schema


def _covered(catalogue: DDLCatalogue) -> dict[str, set[tuple[IndexSpec, ...]]]:
    covered: dict[str, set[tuple[IndexSpec, ...]]] = {}
    for column in columns():
        use_case, _ = split_use_case(column.declare["use_case"])
        assert use_case is not None
        cardinality = column.declare.get("cardinality", DEFAULT_CARDINALITY)
        covered.setdefault(use_case, set()).add(catalogue.specs(use_case, cardinality=cardinality))
    return covered


def test_every_catalogue_index_variant_has_a_pruning_row() -> None:
    catalogue = DDLCatalogue.load()
    covered = _covered(catalogue)
    for use_case in catalogue.use_cases:
        variants = {catalogue.specs(use_case, cardinality=c) for c in CARDINALITIES}
        missing = variants - covered.get(use_case, set())
        assert not missing, f"use case {use_case!r} renders {missing} with no row: add one"


def test_every_vocabulary_use_case_is_in_the_catalogue() -> None:
    """types.yaml names the question and ddl.yaml the answer, so the two lists agree."""
    assert sorted(TypeRegistry.load().use_cases) == DDLCatalogue.load().use_cases


def test_the_meta_schema_the_rows_generate_is_one_the_engine_accepts(tmp_path: Path) -> None:
    """The rows build on the timeseries header with an index per column, so a bad row fails here."""
    meta = tmp_path / "index_use_cases.yaml"
    meta.write_text(yaml.safe_dump(meta_schema(), sort_keys=False), encoding="utf-8")
    source = Source.model_validate(
        {
            "source": "index-probe",
            "match": {"field": "tags.collector.type", "value": "index-probe"},
            "header": {"type": "timeseries", "version": "1.0.3"},
            "schema": {"ttl_days": 0, "engine": "", "meta_schema": str(meta)},
        }
    )
    result = SchemaBuilderV2().build(source)
    assert not result.validation_errors, result.validation_errors
    for column in columns():
        assert f"INDEX idx_{column.name} " in result.create_table_ddl or (
            f"INDEX idx_{column.name}_" in result.create_table_ddl
        ), f"{column.name} renders no index"
    assert len({case.id for case in CASES}) == len(CASES), "two rows share an id"
