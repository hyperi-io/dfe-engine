#  Project:      dfe-engine
#  File:         tests/unit/test_fieldmap/test_remap_view.py
#  Purpose:      Standard-agnostic remap-view model + DDL (Sigma/ECS/CIM)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The unified remap-view engine - ONE builder for every naming standard.

Proves the json-path + cast + keep-base capability that used to live only in
sigma/views.py now serves ECS + CIM (and any future standard) identically. DDL is
asserted as a string (no live ClickHouse).
"""

from __future__ import annotations

import pytest

from dfe_engine.fieldmap.remap_view import (
    RemapColumn,
    RemapViewDefinition,
    RemapViewError,
    _safe_type,
    build_remap_view_ddl,
)


def _ecs_def(**overrides) -> RemapViewDefinition:
    data = {
        "standard": "ecs",
        "source_name": "filebeat_aws",
        "columns": [
            {"field": "host.name", "source_column": "host_name"},
            {"field": "event.action", "json_path": "event.action"},
            {"field": "source.port", "json_path": "source.port", "type": "UInt16"},
        ],
    }
    data.update(overrides)
    return RemapViewDefinition.model_validate(data)


# -- Model validation ----------------------------------------


def test_column_requires_exactly_one_source_kind():
    with pytest.raises(ValueError, match="exactly one"):
        RemapColumn(field="X", source_column="c", json_path="p")
    with pytest.raises(ValueError, match="exactly one"):
        RemapColumn(field="X")


def test_standard_lowercased_and_validated():
    assert RemapViewDefinition(standard="ECS", source_name="s").standard == "ecs"
    with pytest.raises(ValueError, match="standard"):
        RemapViewDefinition(standard="bad name", source_name="s")


def test_json_derived_subset():
    d = _ecs_def()
    assert {c.field for c in d.json_derived_columns} == {"event.action", "source.port"}
    assert d.columns[0].is_json_derived is False


# -- DDL: view name carries the standard (not hard-coded sigma) ----


def test_view_name_uses_standard_suffix():
    ecs = build_remap_view_ddl(_ecs_def(), db="default")
    assert "CREATE OR REPLACE VIEW default.filebeat_aws_ecs AS" in ecs
    cim = build_remap_view_ddl(_ecs_def(standard="cim"), db="default")
    assert "CREATE OR REPLACE VIEW default.filebeat_aws_cim AS" in cim


def test_source_column_alias():
    d = RemapViewDefinition(
        standard="ecs",
        source_name="win",
        columns=[RemapColumn(field="host.name", source_column="host_name")],
        include_source_columns=False,
    )
    ddl = build_remap_view_ddl(d, db="default")
    assert "`host_name` AS `host.name`" in ddl
    assert "FROM default.win;" in ddl
    assert "\n    *" not in ddl  # include_source_columns=False -> no trailing *


def test_json_derived_extract_and_cast():
    ddl = build_remap_view_ddl(_ecs_def(), db="dfe", table_name="filebeat_aws")
    # ECS field lives inside _json -> dynamic-subcolumn extraction (same idiom sigma used)
    assert "assumeNotNull(_json).`event.action` AS `event.action`" in ddl
    assert "CAST(assumeNotNull(_json).`source.port` AS UInt16) AS `source.port`" in ddl
    assert "`host_name` AS `host.name`" in ddl
    # include_source_columns default True -> keep base cols
    assert "    *," in ddl or "    *\n" in ddl


def test_empty_definition_selects_star():
    d = RemapViewDefinition(standard="cim", source_name="win", include_source_columns=False)
    assert "SELECT\n    *\nFROM {db}.win;" in build_remap_view_ddl(d)


def test_default_db_placeholder():
    d = RemapViewDefinition(
        standard="sigma",
        source_name="win",
        columns=[RemapColumn(field="X", source_column="x")],
    )
    ddl = build_remap_view_ddl(d)
    assert "{db}.win_sigma" in ddl
    assert "FROM {db}.win;" in ddl


# -- Injection safety (shared across every standard) ---------


def test_rejects_backtick_in_json_path():
    d = RemapViewDefinition(
        standard="ecs", source_name="win", columns=[RemapColumn(field="X", json_path="a`b")]
    )
    with pytest.raises(RemapViewError, match="backtick"):
        build_remap_view_ddl(d, db="default")


def test_rejects_backtick_in_field_alias():
    d = RemapViewDefinition(
        standard="ecs", source_name="win", columns=[RemapColumn(field="a`b", source_column="x")]
    )
    with pytest.raises(RemapViewError, match="field"):
        build_remap_view_ddl(d, db="default")


def test_rejects_illegal_db_identifier():
    d = RemapViewDefinition(
        standard="ecs", source_name="win", columns=[RemapColumn(field="X", source_column="x")]
    )
    with pytest.raises(RemapViewError, match="db"):
        build_remap_view_ddl(d, db="bad; DROP")


def test_safe_type_allows_real_types_rejects_breakout():
    assert _safe_type("Nullable(UInt16)") == "Nullable(UInt16)"
    assert _safe_type("DateTime64(9)") == "DateTime64(9)"
    with pytest.raises(RemapViewError):
        _safe_type("String) OR (1=1")
    with pytest.raises(RemapViewError):
        _safe_type("Decimal(10, 2")
