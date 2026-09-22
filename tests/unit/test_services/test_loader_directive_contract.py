#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_loader_directive_contract.py
#  Purpose:      Pin the column-COMMENT directive contract between engine and dfe-loader
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The column-COMMENT contract between dfe-engine and dfe-loader.

dfe-engine writes a directive into every column's ClickHouse COMMENT and
dfe-loader parses it back out. Nothing else checks that the two agree, and when
they stopped agreeing the engine kept reporting success while promoted columns
stayed NULL (dfe-engine#459).

The loader's vocabulary is restated here as a literal, because a test against
the engine's own constants only ever checks the engine against itself.
"""

from __future__ import annotations

import pytest

from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.services.schema.json_promotion_service import (
    PromotionRequest,
    build_promotion_columns,
    discover_paths,
    promoted_column_expr,
)
from dfe_engine.source.type_registry import TypeRegistry

# dfe-loader/src/column_meta/mod.rs:379-386 DIRECTIVE_NAMES. A '@' only starts a
# directive when the word after it is one of these; anything else is read as
# prose and leaves the column with no instruction at all.
LOADER_DIRECTIVE_NAMES = frozenset({"skip", "default", "source", "renamed", "computed", "coerce"})


def directive_name(expr: str) -> str:
    """The directive word in ``@name: body``."""
    assert expr.startswith("@"), expr
    return expr[1:].split(":", 1)[0].strip()


def loader_reads_path(comment: str) -> str:
    """The source path dfe-loader resolves from a column COMMENT.

    Mirrors the two rules that bound an ``@source`` body:
    ``strip_path_description`` ends the path at the first ``" - "``
    (dfe-loader/src/column_meta/mod.rs:402) and the remainder splits on ``|``
    into path and fallback (mod.rs:342).
    """
    assert directive_name(comment) == "source", comment
    body = comment.split(":", 1)[1]
    body = body.split(" - ", 1)[0]
    return body.split("|", 1)[0].strip()


class _DiscoverClient:
    """ClickHouse stand-in returning one discovered path."""

    def __init__(self, path: str, ch_type: str = "String") -> None:
        self._rows = [(path, ch_type)]

    def execute(self, sql: str, parameters: dict | None = None) -> list:
        return self._rows if "JSONDynamicPathsWithTypes" in sql else []


PROMOTABLE_PATHS = ["user.email", "probe.promote.value", "id", "CloudTrailEvent.eventName"]


class TestPromotedColumnDirective:
    @pytest.mark.parametrize("path", PROMOTABLE_PATHS)
    def test_directive_name_is_one_the_loader_parses(self, path: str):
        assert directive_name(promoted_column_expr(path)) in LOADER_DIRECTIVE_NAMES

    @pytest.mark.parametrize("path", PROMOTABLE_PATHS)
    def test_written_comment_reads_back_as_the_bare_path(self, path: str):
        outcomes = build_promotion_columns(
            [],
            [PromotionRequest(json_path=path, data_type="string")],
            type_registry=TypeRegistry.default(),
            path_types={},
        )
        column = outcomes[0].column
        generator = DDLGenerator(TypeRegistry.default())
        ddl = generator.generate_alter_add_column("events", column, DDLConfig(db="dfe"))
        comment = ddl.split("COMMENT '", 1)[1].split("'", 1)[0]

        assert loader_reads_path(comment) == path

    @pytest.mark.parametrize("path", PROMOTABLE_PATHS)
    def test_discovery_preview_matches_the_written_column(self, path: str):
        discovered = discover_paths(
            _DiscoverClient(path), db="dfe", source="events", existing_columns=[]
        )
        outcomes = build_promotion_columns(
            [],
            [PromotionRequest(json_path=path)],
            type_registry=TypeRegistry.default(),
            path_types={path: ["String"]},
        )
        assert discovered[0].column_expr == outcomes[0].column.expr
