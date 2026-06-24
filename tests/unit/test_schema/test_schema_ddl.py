"""Tests for the DDL Generator v2 (schema_ddl.py)."""

import pytest

from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry


@pytest.fixture
def registry() -> TypeRegistry:
    return TypeRegistry.default()


@pytest.fixture
def gen(registry: TypeRegistry) -> DDLGenerator:
    return DDLGenerator(registry)


@pytest.fixture
def gen_legacy(registry: TypeRegistry) -> DDLGenerator:
    return DDLGenerator(registry, use_legacy_indexes=True)


# ── Helpers ─────────────────────────────────────────────────────────


def _col(**kwargs) -> SchemaColumn:
    """Shorthand to create a SchemaColumn."""
    return SchemaColumn.model_validate(kwargs)


def _basic_columns() -> list[SchemaColumn]:
    """A minimal set of columns for testing."""
    return [
        _col(name="_timestamp_load", type="timestamp", default="now64(3)", order=0),
        _col(name="_timestamp", type="datetime", use_case="range", order=1),
        _col(
            name="_org_id",
            type="string",
            attribute=["lowcardinality"],
            use_case="dimension",
            order=2,
        ),
        _col(name="user_name", type="string", use_case="dimension"),
        _col(name="message", type="text", use_case="fulltext"),
    ]


# ── CREATE TABLE ────────────────────────────────────────────────────


class TestGenerateCreateTable:
    def test_basic_create_table(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("filebeat", _basic_columns())
        assert "CREATE TABLE IF NOT EXISTS {db}.filebeat" in ddl
        assert "ENGINE = MergeTree()" in ddl
        assert "PARTITION BY toYYYYMMDD(_timestamp_load)" in ddl

    def test_header_comment(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("test_table", _basic_columns())
        assert ddl.startswith("-- ===")
        assert "-- DFE Schema DDL: test_table" in ddl

    def test_no_generated_at_header_by_default(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        assert "-- Generated at:" not in ddl

    def test_generated_at_header_when_provided(self, gen: DDLGenerator):
        ddl = gen.generate_create_table(
            "t", _basic_columns(), generated_time="2026-01-02 03:04:05 UTC"
        )
        assert "-- Generated at: 2026-01-02 03:04:05 UTC" in ddl

    def test_column_definitions(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        # timestamp — not null by default
        assert "`_timestamp_load` DateTime64(3,'UTC')" in ddl
        assert "DEFAULT now64(3)" in ddl
        # datetime — nullable by default
        assert "`_timestamp` Nullable(DateTime64(3,'UTC'))" in ddl
        # string with lowcardinality
        assert "`_org_id` LowCardinality(Nullable(String))" in ddl
        # text for message
        assert "`message` Nullable(String)" in ddl

    def test_codec_assignment(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        # timestamp primitive → Delta, LZ4
        assert "CODEC(Delta, LZ4)" in ddl
        # string primitive → ZSTD(1)
        assert "CODEC(ZSTD(1))" in ddl
        # text primitive → ZSTD(3)
        assert "CODEC(ZSTD(3))" in ddl

    def test_index_generation(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        assert "INDEX idx__timestamp `_timestamp` TYPE minmax GRANULARITY 4" in ddl
        assert "INDEX idx__org_id `_org_id` TYPE set(0) GRANULARITY 4" in ddl
        assert "INDEX idx_user_name `user_name` TYPE set(0) GRANULARITY 4" in ddl
        assert (
            "INDEX idx_message `message` TYPE text(tokenizer=splitByNonAlpha) GRANULARITY 1" in ddl
        )

    def test_legacy_indexes(self, gen_legacy: DDLGenerator):
        ddl = gen_legacy.generate_create_table("t", _basic_columns())
        assert "TYPE tokenbf_v1(8192, 4, 0)" in ddl
        assert "TYPE text(" not in ddl

    def test_order_by(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        assert "ORDER BY (`_timestamp_load`)" in ddl
        assert "PRIMARY KEY (`_timestamp_load`)" in ddl

    def test_order_by_keeps_not_null_columns(self, gen: DDLGenerator):
        cols = [
            _col(name="a", type="string", attribute=["not_null"], order=0),
            _col(name="b", type="integer", attribute=["not_null"], order=1),
        ]
        ddl = gen.generate_create_table("t", cols)
        assert "ORDER BY (`a`, `b`)" in ddl
        assert "PRIMARY KEY (`a`, `b`)" in ddl

    def test_order_by_empty(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string")]
        ddl = gen.generate_create_table("t", cols)
        assert "ORDER BY tuple()" in ddl
        assert "PRIMARY KEY" not in ddl

    def test_ttl(self, gen: DDLGenerator):
        cfg = DDLConfig(ttl_days=30)
        ddl = gen.generate_create_table("t", _basic_columns(), cfg)
        assert "_timestamp + INTERVAL 30 DAY DELETE WHERE _timestamp >= 0" in ddl
        assert "_timestamp_load + INTERVAL 30 DAY DELETE WHERE _timestamp_load >= 0" in ddl

    def test_ttl_none(self, gen: DDLGenerator):
        cfg = DDLConfig(ttl_days=None)
        ddl = gen.generate_create_table("t", _basic_columns(), cfg)
        assert "TTL" not in ddl

    def test_engine_config(self, gen: DDLGenerator):
        cfg = DDLConfig(engine="ReplicatedMergeTree")
        ddl = gen.generate_create_table("t", _basic_columns(), cfg)
        assert "ENGINE = ReplicatedMergeTree()" in ddl

    def test_cluster(self, gen: DDLGenerator):
        cfg = DDLConfig(cluster="my_cluster")
        ddl = gen.generate_create_table("t", _basic_columns(), cfg)
        assert "ON CLUSTER my_cluster" in ddl

    def test_sample_by(self, gen: DDLGenerator):
        cfg = DDLConfig(sample_by="cityHash64(_timestamp_load)")
        ddl = gen.generate_create_table("t", _basic_columns(), cfg)
        assert "SAMPLE BY cityHash64(_timestamp_load)" in ddl

    def test_table_comment(self, gen: DDLGenerator):
        cfg = DDLConfig(profile_name="timeseries", profile_version="1.0.0", schema_version="2")
        ddl = gen.generate_create_table("t", _basic_columns(), cfg)
        assert "@t_version: 2" in ddl
        assert "@profile: timeseries" in ddl
        assert "@profile_version: 1.0.0" in ddl

    def test_settings(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        assert "index_granularity = 2048" in ddl
        assert "ttl_only_drop_parts = 1" in ddl

    def test_projection(self, gen: DDLGenerator):
        ddl = gen.generate_create_table("t", _basic_columns())
        assert "PROJECTION _timestamp_optimized (SELECT * ORDER BY `_timestamp`)" in ddl

    def test_projection_skipped_when_column_missing(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string")]
        ddl = gen.generate_create_table("t", cols)
        assert "PROJECTION" not in ddl


# ── Column Types ────────────────────────────────────────────────────


class TestColumnTypes:
    def test_ch_override(self, gen: DDLGenerator):
        cols = [_col(name="status", type="integer", ch_override="UInt16", use_case="dimension")]
        ddl = gen.generate_create_table("t", cols)
        assert "`status` UInt16" in ddl

    def test_ch_override_no_codec(self, gen: DDLGenerator):
        cols = [_col(name="x", type="integer", ch_override="UInt16")]
        ddl = gen.generate_create_table("t", cols)
        # ch_override suppresses codec
        assert "CODEC" not in ddl or "UInt16 CODEC" not in ddl

    def test_nullable_wrapping(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string")]
        ddl = gen.generate_create_table("t", cols)
        assert "Nullable(String)" in ddl

    def test_not_null_attribute(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", attribute=["not_null"])]
        ddl = gen.generate_create_table("t", cols)
        assert "`x` String" in ddl
        assert "Nullable" not in ddl.split("`x`")[1].split("\n")[0]

    def test_lowcardinality(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", attribute=["lowcardinality"])]
        ddl = gen.generate_create_table("t", cols)
        assert "LowCardinality(Nullable(String))" in ddl

    def test_lowcardinality_not_null(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", attribute=["lowcardinality", "not_null"])]
        ddl = gen.generate_create_table("t", cols)
        assert "LowCardinality(String)" in ddl
        assert "Nullable" not in ddl.split("`x`")[1].split("\n")[0]

    def test_boolean_not_null_by_default(self, gen: DDLGenerator):
        cols = [_col(name="active", type="boolean")]
        ddl = gen.generate_create_table("t", cols)
        assert "`active` Bool" in ddl
        assert "Nullable" not in ddl.split("`active`")[1].split("\n")[0]

    def test_ip_type(self, gen: DDLGenerator):
        cols = [_col(name="src_ip", type="ip", use_case="range")]
        ddl = gen.generate_create_table("t", cols)
        assert "Nullable(IPv6)" in ddl
        assert "INDEX idx_src_ip" in ddl
        assert "TYPE minmax GRANULARITY 4" in ddl

    def test_uuid_type(self, gen: DDLGenerator):
        cols = [_col(name="trace_id", type="uuid", use_case="bloom")]
        ddl = gen.generate_create_table("t", cols)
        assert "Nullable(UUID)" in ddl
        assert "INDEX idx_trace_id" in ddl
        assert "TYPE bloom_filter GRANULARITY 4" in ddl

    def test_json_type(self, gen: DDLGenerator):
        cols = [_col(name="data", type="json")]
        ddl = gen.generate_create_table("t", cols)
        assert "Nullable(JSON)" in ddl


# ── DEFAULT / MATERIALIZED / ALIAS ──────────────────────────────────


class TestExpressions:
    def test_default_expression(self, gen: DDLGenerator):
        cols = [_col(name="ts", type="timestamp", default="now64(3)")]
        ddl = gen.generate_create_table("t", cols)
        assert "DEFAULT now64(3)" in ddl

    def test_materialized_expression(self, gen: DDLGenerator):
        cols = [
            _col(name="day", type="date", attribute=["materialized"], default="toDate(_timestamp)")
        ]
        ddl = gen.generate_create_table("t", cols)
        assert "MATERIALIZED toDate(_timestamp)" in ddl
        assert "DEFAULT" not in ddl.split("`day`")[1].split("\n")[0]

    def test_alias_expression(self, gen: DDLGenerator):
        cols = [
            _col(name="hour", type="integer", attribute=["alias"], default="toHour(_timestamp)")
        ]
        ddl = gen.generate_create_table("t", cols)
        assert "ALIAS toHour(_timestamp)" in ddl

    def test_no_default(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string")]
        ddl = gen.generate_create_table("t", cols)
        line = next(ln for ln in ddl.split("\n") if "`x`" in ln)
        assert "DEFAULT" not in line
        assert "MATERIALIZED" not in line
        assert "ALIAS" not in line


# ── Comments ────────────────────────────────────────────────────────


class TestComments:
    def test_expr_only(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", expr="@source: user_id")]
        ddl = gen.generate_create_table("t", cols)
        assert "COMMENT '@source: user_id'" in ddl

    def test_comment_only(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", comment="User identifier")]
        ddl = gen.generate_create_table("t", cols)
        assert "COMMENT 'User identifier'" in ddl

    def test_expr_and_comment_combined(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", expr="@source: user_id", comment="User identifier")]
        ddl = gen.generate_create_table("t", cols)
        assert "COMMENT '@source: user_id - User identifier'" in ddl

    def test_column_comment_escaped(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string", comment="it's a test")]
        ddl = gen.generate_create_table("t", cols)
        assert "COMMENT 'it\\'s a test'" in ddl

    def test_no_comment(self, gen: DDLGenerator):
        cols = [_col(name="x", type="string")]
        ddl = gen.generate_create_table("t", cols)
        line = next(ln for ln in ddl.split("\n") if "`x`" in ln)
        assert "COMMENT" not in line


# ── Enum ────────────────────────────────────────────────────────────


class TestEnum:
    def test_enum_from_default(self, gen: DDLGenerator):
        cols = [_col(name="status", type="enum", default="'active'=1, 'inactive'=2")]
        ddl = gen.generate_create_table("t", cols)
        assert "Enum8('active'=1, 'inactive'=2)" in ddl

    def test_enum_with_ch_override(self, gen: DDLGenerator):
        cols = [_col(name="status", type="enum", ch_override="Enum16('a'=1, 'b'=2, 'c'=3)")]
        ddl = gen.generate_create_table("t", cols)
        assert "Enum16('a'=1, 'b'=2, 'c'=3)" in ddl


# ── Use Cases → Indexes ─────────────────────────────────────────────


class TestUseCaseIndexes:
    def test_dimension_index(self, gen: DDLGenerator):
        cols = [_col(name="cat", type="string", use_case="dimension")]
        ddl = gen.generate_create_table("t", cols)
        assert "INDEX idx_cat `cat` TYPE set(0) GRANULARITY 4" in ddl

    def test_fulltext_index(self, gen: DDLGenerator):
        cols = [_col(name="msg", type="text", use_case="fulltext")]
        ddl = gen.generate_create_table("t", cols)
        assert "INDEX idx_msg `msg` TYPE text(tokenizer=splitByNonAlpha) GRANULARITY 1" in ddl

    def test_text_search_index(self, gen: DDLGenerator):
        cols = [_col(name="body", type="text", use_case="text_search")]
        ddl = gen.generate_create_table("t", cols)
        assert "INDEX idx_body `body` TYPE text(tokenizer=ngrams(3)) GRANULARITY 1" in ddl

    def test_range_index(self, gen: DDLGenerator):
        cols = [_col(name="latency", type="float", use_case="range")]
        ddl = gen.generate_create_table("t", cols)
        assert "INDEX idx_latency `latency` TYPE minmax GRANULARITY 4" in ddl

    def test_bloom_index(self, gen: DDLGenerator):
        cols = [_col(name="req_id", type="string", use_case="bloom")]
        ddl = gen.generate_create_table("t", cols)
        assert "INDEX idx_req_id `req_id` TYPE bloom_filter GRANULARITY 4" in ddl

    def test_no_use_case_no_index(self, gen: DDLGenerator):
        cols = [_col(name="payload", type="string")]
        ddl = gen.generate_create_table("t", cols)
        assert "INDEX idx_payload" not in ddl

    def test_legacy_fulltext(self, gen_legacy: DDLGenerator):
        cols = [_col(name="msg", type="text", use_case="fulltext")]
        ddl = gen_legacy.generate_create_table("t", cols)
        assert "tokenbf_v1(8192, 4, 0)" in ddl

    def test_legacy_text_search(self, gen_legacy: DDLGenerator):
        cols = [_col(name="body", type="text", use_case="text_search")]
        ddl = gen_legacy.generate_create_table("t", cols)
        assert "ngrambf_v1(3, 256, 2, 0)" in ddl


# ── ALTER TABLE ─────────────────────────────────────────────────────


class TestAlterTable:
    def test_add_column(self, gen: DDLGenerator):
        col = _col(name="new_field", type="string", use_case="dimension")
        ddl = gen.generate_alter_add_column("filebeat", col)
        assert "ALTER TABLE {db}.filebeat ADD COLUMN IF NOT EXISTS" in ddl
        assert "`new_field` Nullable(String)" in ddl

    def test_add_column_after(self, gen: DDLGenerator):
        col = _col(name="new_field", type="string")
        ddl = gen.generate_alter_add_column("t", col, after="user_name")
        assert "AFTER `user_name`" in ddl

    def test_modify_column(self, gen: DDLGenerator):
        col = _col(name="user_name", type="string", attribute=["lowcardinality"])
        ddl = gen.generate_alter_modify_column("filebeat", col)
        assert "ALTER TABLE {db}.filebeat MODIFY COLUMN" in ddl
        assert "LowCardinality(Nullable(String))" in ddl


# ── Standard Views (generic) ─────────────────────────────────────────


class TestGenerateView:
    def test_generic_view_with_suffix(self, gen: DDLGenerator):
        mappings = {"source.ip": "source_ip", "user.name": "user_name"}
        ddl = gen.generate_view("syslog", mappings, "ecs")
        assert "CREATE OR REPLACE VIEW {db}.syslog_ecs AS" in ddl
        assert "`source_ip` AS `source.ip`" in ddl
        assert "`user_name` AS `user.name`" in ddl
        assert "FROM {db}.syslog" in ddl

    def test_cim_suffix(self, gen: DDLGenerator):
        ddl = gen.generate_view("t", {"src_ip": "source_ip"}, "cim")
        assert "t_cim" in ddl

    def test_sigma_via_generic(self, gen: DDLGenerator):
        """generate_view with suffix='sigma' matches generate_sigma_view output."""
        mappings = {"EventID": "event_id"}
        via_generic = gen.generate_view("t", mappings, "sigma")
        via_sigma = gen.generate_sigma_view("t", mappings)
        assert via_generic == via_sigma


# ── Sigma View ──────────────────────────────────────────────────────


class TestSigmaView:
    def test_basic_sigma_view(self, gen: DDLGenerator):
        mappings = {
            "SourceIP": "source_ip",
            "User": "user_name",
            "EventID": "event_id",
        }
        ddl = gen.generate_sigma_view("windows_audit", mappings)
        assert "CREATE OR REPLACE VIEW {db}.windows_audit_sigma AS" in ddl
        assert "`event_id` AS `EventID`" in ddl
        assert "`source_ip` AS `SourceIP`" in ddl
        assert "`user_name` AS `User`" in ddl
        assert "FROM {db}.windows_audit" in ddl

    def test_sigma_view_includes_star(self, gen: DDLGenerator):
        ddl = gen.generate_sigma_view("t", {"X": "x"})
        assert "*" in ddl

    def test_sigma_view_empty_mappings(self, gen: DDLGenerator):
        ddl = gen.generate_sigma_view("t", {})
        assert "SELECT\n    *" in ddl

    def test_sigma_view_custom_db(self, gen: DDLGenerator):
        cfg = DDLConfig(db="mydb")
        ddl = gen.generate_sigma_view("t", {"X": "x"}, cfg)
        assert "mydb.t_sigma" in ddl
        assert "FROM mydb.t" in ddl


# ── DDLConfig ───────────────────────────────────────────────────────


class TestDDLConfig:
    def test_defaults(self):
        cfg = DDLConfig()
        assert cfg.db == "{db}"
        assert cfg.engine == "MergeTree"
        assert cfg.ttl_days == 90
        assert cfg.partition_column == "_timestamp_load"
        assert cfg.index_granularity == 2048
        assert cfg.ttl_only_drop_parts is True
        assert cfg.cluster is None
        assert cfg.schema_version is None

    def test_custom_config(self):
        cfg = DDLConfig(
            db="analytics",
            engine="SharedMergeTree",
            ttl_days=365,
            profile_name="minimal",
        )
        assert cfg.db == "analytics"
        assert cfg.engine == "SharedMergeTree"
        assert cfg.ttl_days == 365
        assert cfg.profile_name == "minimal"
