#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_manifest_index_moves.py
#  Purpose:      A manifest table whose use-case index changed gets the new one on upgrade
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An existing manifest table is brought to the index its definition now renders.

set(0) prunes nothing on a Nullable column on ClickHouse 26.3, so the DDL
catalogue answers one with bloom_filter. A table created before that change
still carries set(0) until the applier drops it and adds the new one. Indexes
a table declares for itself are left alone.

A fake client stands in for ClickHouse and records every statement it is told
to run.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from dfe_schemas.clickhouse import Platform
from dfe_schemas.render import RenderedObject

from dfe_engine.schema.applier import ManifestApplier

_CREATE = """CREATE TABLE IF NOT EXISTS `dfe`.`main`
(
    `_timestamp_load` DateTime64(3,'UTC'),
    `_source` LowCardinality(Nullable(String)),
    `attrs` Map(LowCardinality(String), String),
    INDEX `idx__source` `_source` TYPE bloom_filter GRANULARITY 4,
    INDEX idx_attr_key mapKeys(attrs) TYPE bloom_filter(0.01) GRANULARITY 1
)
ENGINE = ReplicatedMergeTree
ORDER BY (`_timestamp_load`)
SETTINGS
    index_granularity = 2048"""


@dataclass
class _Result:
    result_rows: list


@dataclass
class _LiveTable:
    """One live table with the given indexes. Every statement is recorded."""

    indexes: list[tuple[str, str, int]]
    statements: list[str] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "data_skipping_indices" in sql:
            return _Result(list(self.indexes))
        if "sorting_key" in sql:
            return _Result(
                [["_timestamp_load", "", "ReplicatedMergeTree ORDER BY _timestamp_load"]]
            )
        if "system.columns" in sql:
            return _Result(
                [
                    ("_timestamp_load", "DateTime64(3, 'UTC')"),
                    ("_source", "LowCardinality(Nullable(String))"),
                    ("attrs", "Map(LowCardinality(String), String)"),
                ]
            )
        if "engine_full" in sql:
            return _Result([["main", "ReplicatedMergeTree", "ReplicatedMergeTree"]])
        return _Result([[1]])

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.statements.append(sql)


def _rendered() -> RenderedObject:
    return RenderedObject(
        id="data.main",
        kind="table",
        database="dfe",
        name="main",
        platform="self_hosted",
        additive=True,
        optional=False,
        statements=(_CREATE,),
        checksum="new",
    )


@pytest.mark.parametrize(
    ("live_source_index", "moved"),
    [
        pytest.param(("idx__source", "set(0)", 4), True, id="set_to_bloom"),
        pytest.param(("idx__source", "bloom_filter", 4), False, id="already_bloom"),
    ],
)
def test_a_changed_use_case_index_is_dropped_and_added_again(
    live_source_index: tuple[str, str, int], moved: bool
):
    client = _LiveTable(indexes=[live_source_index])
    applier = ManifestApplier(client, Platform.SELF_HOSTED, schemas_version="test")

    outcome = applier.apply(_rendered())

    source_moves = [s for s in client.statements if "idx__source" in s]
    if moved:
        assert source_moves == [
            "ALTER TABLE `dfe`.`main` DROP INDEX IF EXISTS idx__source",
            "ALTER TABLE `dfe`.`main` ADD INDEX `idx__source` `_source` TYPE bloom_filter GRANULARITY 4",
        ]
        assert outcome.action == "altered"
    else:
        assert source_moves == []
        assert outcome.action == "unchanged"


def test_an_index_the_table_declares_for_itself_is_left_alone():
    """idx_attr_key is the table's own, not a use case's, so a missing one is not added here."""
    client = _LiveTable(indexes=[("idx__source", "bloom_filter", 4)])

    ManifestApplier(client, Platform.SELF_HOSTED, schemas_version="test").apply(_rendered())

    assert not [s for s in client.statements if "idx_attr_key" in s]
