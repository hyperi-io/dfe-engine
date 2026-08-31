#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_table_loader.py
#  Purpose:      Pin the tables/ reader, incl. the defaults a TTL would ruin
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The otel and engine-state tables are defined in dfe-schemas, not here.

The reader has to reproduce what the Python specs did exactly. Two of those
behaviours are invisible until a deploy goes wrong: TTL and projection default
to OFF rather than to the data-table defaults, and a missing schemas tree
raises instead of yielding an empty table list.
"""

from __future__ import annotations

import pytest

from dfe_engine.schema.schema_loader import SchemaLoadError
from dfe_engine.schema.table_loader import load_table_spec, load_view_ddl

TABLE_YAML = """\
current: "1.0.0"
versions:
  "1.0.0":
    table:
      name: "widget"
      engine: "ReplacingMergeTree(updated)"
      partition_by: "toDate(_timestamp_load)"
      order_by: "a, b"
      index_granularity: 8192
      indexes:
        - "INDEX idx_a a TYPE minmax GRANULARITY 1"
    columns:
      - name: "a"
        ch_type: "String"
        lowcardinality: true
        order: 0
        codec: "ZSTD(1)"
        comment: "first key"
      - name: "b"
        ch_type: "DateTime64(3)"
        default: "now64(3)"
      - name: "c"
        ch_type: "String"
        materialized: "lower(a)"
      - name: "j"
        ch_type: "JSON"
        max_dynamic_paths: inherit
"""

VIEW_YAML = """\
current: "1.0.0"
versions:
  "1.0.0":
    table:
      name: "widget_target"
      engine: "MergeTree"
    materialized_view:
      name: "widget_mv"
      to: "widget_target"
      select: |-
        SELECT a FROM {db}.widget
    columns:
      - name: "a"
        ch_type: "String"
"""


@pytest.fixture
def schemas_root(tmp_path, monkeypatch):
    """A schemas tree holding the two definitions above."""
    (tmp_path / "common-header").mkdir()
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "widget.yaml").write_text(TABLE_YAML, encoding="utf-8")
    (tables / "view.yaml").write_text(VIEW_YAML, encoding="utf-8")
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path))
    return tmp_path


class TestColumns:
    """Each YAML key lands on the SchemaColumn field the generator reads."""

    def test_exact_type_and_lowcardinality_split(self, schemas_root):
        col = load_table_spec("tables/widget", "dfe").columns[0]
        assert col.ch_override == "String"
        assert col.attribute == ["lowcardinality"]
        assert col.order == 0
        assert col.codec == "ZSTD(1)"
        assert col.comment == "first key"

    def test_default_is_a_default(self, schemas_root):
        col = load_table_spec("tables/widget", "dfe").columns[1]
        assert col.default == "now64(3)"
        assert "materialized" not in col.attribute

    def test_materialized_carries_its_expression(self, schemas_root):
        col = load_table_spec("tables/widget", "dfe").columns[2]
        assert col.attribute == ["materialized"]
        assert col.default == "lower(a)"

    def test_inherit_resolves_to_a_number(self, schemas_root):
        col = load_table_spec("tables/widget", "dfe").columns[3]
        assert isinstance(col.max_dynamic_paths, int)
        assert col.max_dynamic_paths > 0

    def test_both_default_and_materialized_is_an_error(self, tmp_path, monkeypatch):
        (tmp_path / "common-header").mkdir()
        tables = tmp_path / "tables"
        tables.mkdir()
        (tables / "bad.yaml").write_text(
            'current: "1.0.0"\nversions:\n  "1.0.0":\n'
            '    table:\n      name: "bad"\n'
            '    columns:\n      - name: "a"\n        ch_type: "String"\n'
            '        default: "1"\n        materialized: "2"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path))
        with pytest.raises(SchemaLoadError, match="both 'default' and 'materialized'"):
            load_table_spec("tables/bad", "dfe")


class TestConfig:
    """The clause block, and the defaults that must stay off."""

    def test_clauses_are_taken_as_written(self, schemas_root):
        cfg = load_table_spec("tables/widget", "dfe").config
        assert cfg.db == "dfe"
        assert cfg.engine == "ReplacingMergeTree(updated)"
        assert cfg.partition_by == "toDate(_timestamp_load)"
        assert cfg.order_by == "a, b"
        assert cfg.index_granularity == 8192
        assert cfg.extra_indexes == ["INDEX idx_a a TYPE minmax GRANULARITY 1"]

    def test_ttl_and_projection_default_to_off(self, schemas_root):
        """DDLConfig defaults a 90-day TTL and a projection these cannot use."""
        cfg = load_table_spec("tables/widget", "dfe").config
        assert cfg.ttl_days is None
        assert cfg.ttl_columns == []
        assert cfg.projection_order_by is None


class TestResolution:
    """Where the definitions come from, and what happens when they do not."""

    def test_the_table_names_itself(self, schemas_root):
        assert load_table_spec("tables/widget", "dfe").name == "widget"

    def test_a_definition_that_names_no_table_is_an_error(self, tmp_path, monkeypatch):
        (tmp_path / "common-header").mkdir()
        tables = tmp_path / "tables"
        tables.mkdir()
        (tables / "anon.yaml").write_text(
            'current: "1.0.0"\nversions:\n  "1.0.0":\n'
            '    table:\n      engine: "MergeTree"\n'
            '    columns:\n      - name: "a"\n        ch_type: "String"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path))
        with pytest.raises(SchemaLoadError, match="does not name its table"):
            load_table_spec("tables/anon", "dfe")

    def test_a_missing_tree_raises_rather_than_yielding_nothing(self, monkeypatch, tmp_path):
        """An empty table list would let the data plane start on an empty db."""
        monkeypatch.delenv("DFE_SCHEMAS_DIR", raising=False)
        monkeypatch.setenv("DFE_SCHEMAS_SEED_DIR", str(tmp_path / "absent"))
        monkeypatch.setattr(
            "dfe_engine.schema.schema_loader._resolve_package_schemas_root", lambda: None
        )
        with pytest.raises(SchemaLoadError, match="Cannot resolve the dfe-schemas tree"):
            load_table_spec("tables/widget", "dfe")

    def test_a_missing_file_names_the_path(self, schemas_root):
        with pytest.raises(SchemaLoadError, match="Table definition not found"):
            load_table_spec("tables/nope", "dfe")


class TestView:
    """A materialised view has a SELECT and a target, not columns."""

    def test_the_database_is_substituted(self, schemas_root):
        name, ddl = load_view_ddl("tables/view", "dfe")
        assert name == "widget_mv"
        assert "TO dfe.widget_target AS" in ddl
        assert "FROM dfe.widget" in ddl

    def test_on_cluster_lands_on_the_view_name(self, schemas_root):
        _, ddl = load_view_ddl("tables/view", "dfe", " ON CLUSTER default")
        assert "dfe.widget_mv ON CLUSTER default TO" in ddl

    def test_a_table_without_one_returns_none(self, schemas_root):
        assert load_view_ddl("tables/widget", "dfe") is None
