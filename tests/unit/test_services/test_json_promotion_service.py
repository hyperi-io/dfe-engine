#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_json_promotion_service.py
#  Purpose:      Tests for JSON field promotion service (pure functions)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from dfe_engine.schema.models import SchemaColumn as MetaSchemaColumn
from dfe_engine.services.schema.json_promotion_service import (
    PromotionRequest,
    build_promotion_columns,
    ch_dynamic_type_to_primitive,
    copy_cel_for_path,
    discover_paths,
    list_promoted_json_fields,
    promoted_paths,
    promotion_preview_ddl,
    qualified_table,
    sample_rows,
    suggested_column_name,
)
from dfe_engine.source.type_registry import TypeRegistry


def make_meta_schema_column(name: str, type: str = "string", **kwargs: object) -> MetaSchemaColumn:
    """Build a schema.models.SchemaColumn (meta-schema layer)."""
    return MetaSchemaColumn.model_validate({"name": name, "type": type, **kwargs})


def make_promotion_request(json_path: str, **kwargs: object) -> PromotionRequest:
    """Build a PromotionRequest."""
    return PromotionRequest(json_path=json_path, **kwargs)


# ── ch_dynamic_type_to_primitive ─────────────────────────────


class ChTypeCase(TypedDict):
    id: str
    ch_type: str
    expected: tuple[str, list[str]]


CH_TYPE_CASES: list[ChTypeCase] = [
    {"id": "string", "ch_type": "String", "expected": ("string", [])},
    {"id": "fixed_string", "ch_type": "FixedString(16)", "expected": ("string", [])},
    {"id": "int64", "ch_type": "Int64", "expected": ("integer", [])},
    {"id": "uint8", "ch_type": "UInt8", "expected": ("integer", [])},
    {"id": "float64", "ch_type": "Float64", "expected": ("float", [])},
    {"id": "bool", "ch_type": "Bool", "expected": ("boolean", [])},
    {"id": "datetime64", "ch_type": "DateTime64(3,'UTC')", "expected": ("datetime", [])},
    {"id": "date", "ch_type": "Date", "expected": ("date", [])},
    {"id": "uuid", "ch_type": "UUID", "expected": ("uuid", [])},
    {"id": "ipv6", "ch_type": "IPv6", "expected": ("ip", [])},
    {"id": "nullable_string", "ch_type": "Nullable(String)", "expected": ("string", [])},
    {"id": "nullable_int", "ch_type": "Nullable(Int64)", "expected": ("integer", [])},
    {"id": "lowcard_string", "ch_type": "LowCardinality(String)", "expected": ("string", [])},
    {"id": "array_fallback", "ch_type": "Array(String)", "expected": ("json", ["nullable"])},
    {"id": "tuple_fallback", "ch_type": "Tuple(String, Int64)", "expected": ("json", ["nullable"])},
    {"id": "map_fallback", "ch_type": "Map(String, String)", "expected": ("json", ["nullable"])},
    {"id": "unknown_fallback", "ch_type": "WeirdType", "expected": ("json", ["nullable"])},
]


class TestChDynamicTypeToPrimitive:
    @pytest.mark.parametrize("case", CH_TYPE_CASES, ids=[c["id"] for c in CH_TYPE_CASES])
    def test_maps(self, case: ChTypeCase):
        assert ch_dynamic_type_to_primitive(case["ch_type"]) == case["expected"]


# ── suggested_column_name ────────────────────────────────────


class SuggestedNameCase(TypedDict):
    id: str
    path: str
    existing: set[str]
    expected: str


SUGGESTED_NAME_CASES: list[SuggestedNameCase] = [
    {"id": "dotted", "path": "user.email", "existing": set(), "expected": "user_email"},
    {"id": "camel", "path": "userName", "existing": set(), "expected": "user_name"},
    {"id": "double_dot", "path": "a..b", "existing": set(), "expected": "a_b"},
    {"id": "leading_digit", "path": "123abc", "existing": set(), "expected": "f_123abc"},
    {
        "id": "collision_suffix",
        "path": "user.email",
        "existing": {"user_email"},
        "expected": "user_email_2",
    },
    {
        "id": "collision_double",
        "path": "user.email",
        "existing": {"user_email", "user_email_2"},
        "expected": "user_email_3",
    },
]


class TestSuggestedColumnName:
    @pytest.mark.parametrize(
        "case", SUGGESTED_NAME_CASES, ids=[c["id"] for c in SUGGESTED_NAME_CASES]
    )
    def test_derives(self, case: SuggestedNameCase):
        assert suggested_column_name(case["path"], case["existing"]) == case["expected"]


# ── copy_cel_for_path / qualified_table ──────────────────────


class CopyCelCase(TypedDict):
    id: str
    path: str
    expected: str


COPY_CEL_CASES: list[CopyCelCase] = [
    {"id": "nested", "path": "user.email", "expected": "_json.user.email"},
    {"id": "flat", "path": "id", "expected": "_json.id"},
]


class TestCopyCelForPath:
    @pytest.mark.parametrize("case", COPY_CEL_CASES, ids=[c["id"] for c in COPY_CEL_CASES])
    def test_builds(self, case: CopyCelCase):
        assert copy_cel_for_path(case["path"]) == case["expected"]


class QualifiedTableCase(TypedDict):
    id: str
    db: str
    source: str
    expected: str


QUALIFIED_TABLE_CASES: list[QualifiedTableCase] = [
    {"id": "default", "db": "default", "source": "filebeat", "expected": "`default`.`filebeat`"},
    {"id": "custom_db", "db": "dfe", "source": "aws_ct", "expected": "`dfe`.`aws_ct`"},
]


class TestQualifiedTable:
    @pytest.mark.parametrize(
        "case", QUALIFIED_TABLE_CASES, ids=[c["id"] for c in QUALIFIED_TABLE_CASES]
    )
    def test_quotes(self, case: QualifiedTableCase):
        assert qualified_table(case["db"], case["source"]) == case["expected"]


# ── promoted_paths ───────────────────────────────────────────


class PromotedPathsCase(TypedDict):
    id: str
    columns: list[MetaSchemaColumn]
    expected: dict[str, str]


PROMOTED_PATHS_CASES: list[PromotedPathsCase] = [
    {
        "id": "copy_directive",
        "columns": [make_meta_schema_column(name="user_email", expr="@copy: _json.user.email")],
        "expected": {"user.email": "user_email"},
    },
    {
        "id": "copy_without_prefix",
        "columns": [make_meta_schema_column(name="uid", expr="@copy: user.id")],
        "expected": {"user.id": "uid"},
    },
    {
        "id": "non_copy_expr_ignored",
        "columns": [make_meta_schema_column(name="foo", expr="@source: foo")],
        "expected": {},
    },
    {
        "id": "no_expr_ignored",
        "columns": [make_meta_schema_column(name="bar")],
        "expected": {},
    },
]


class TestPromotedPaths:
    @pytest.mark.parametrize(
        "case", PROMOTED_PATHS_CASES, ids=[c["id"] for c in PROMOTED_PATHS_CASES]
    )
    def test_extracts(self, case: PromotedPathsCase):
        assert promoted_paths(case["columns"]) == case["expected"]


class TestListPromotedJsonFields:
    def test_maps_to_name_and_key(self):
        cols = [
            make_meta_schema_column(
                name="cloud_trail_event_tls_details_cipher_suite",
                expr="@copy: _json.CloudTrailEvent.tlsDetails.cipherSuite",
            )
        ]
        assert list_promoted_json_fields(cols) == [
            {
                "name": "cloud_trail_event_tls_details_cipher_suite",
                "key": "_json.CloudTrailEvent.tlsDetails.cipherSuite",
            }
        ]


# ── build_promotion_columns ──────────────────────────────────

_JSON_COL = make_meta_schema_column(name="_json", type="json")


def _project(outcomes: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "json_path": o.json_path,
            "status": o.status,
            "column_name": o.column_name,
            "data_type": o.data_type,
            "index_type": o.index_type,
            "copy_cel": o.copy_cel,
            "error": o.error,
        }
        for o in outcomes
    ]


class BuildCase(TypedDict):
    id: str
    existing: list[MetaSchemaColumn]
    requests: list[PromotionRequest]
    path_types: dict[str, list[str]]
    expected: list[dict[str, Any]]


BUILD_CASES: list[BuildCase] = [
    {
        "id": "single_ok_autoderive",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.email")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "ok",
                "column_name": "user_email",
                "data_type": "string",
                "index_type": None,
                "copy_cel": "_json.user.email",
                "error": None,
            }
        ],
    },
    {
        "id": "explicit_type_skips_discovery",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.score", data_type="integer")],
        "path_types": {},
        "expected": [
            {
                "json_path": "user.score",
                "status": "ok",
                "column_name": "user_score",
                "data_type": "integer",
                "index_type": None,
                "copy_cel": "_json.user.score",
                "error": None,
            }
        ],
    },
    {
        "id": "explicit_column_name",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.email", column_name="email")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "ok",
                "column_name": "email",
                "data_type": "string",
                "index_type": None,
                "copy_cel": "_json.user.email",
                "error": None,
            }
        ],
    },
    {
        "id": "with_index_type",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.email", index_type="bloom_filter")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "ok",
                "column_name": "user_email",
                "data_type": "string",
                "index_type": "bloom_filter",
                "copy_cel": "_json.user.email",
                "error": None,
            }
        ],
    },
    {
        "id": "already_promoted",
        "existing": [
            _JSON_COL,
            make_meta_schema_column(name="user_email", expr="@copy: _json.user.email"),
        ],
        "requests": [make_promotion_request("user.email")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "index_type": None,
                "copy_cel": None,
                "error": "path already promoted to column 'user_email'",
            }
        ],
    },
    {
        "id": "explicit_name_collision",
        "existing": [_JSON_COL, make_meta_schema_column(name="email")],
        "requests": [make_promotion_request("user.email", column_name="email")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "index_type": None,
                "copy_cel": None,
                "error": "column name 'email' already exists",
            }
        ],
    },
    {
        "id": "type_conflict",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.x")],
        "path_types": {"user.x": ["String", "Int64"]},
        "expected": [
            {
                "json_path": "user.x",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "index_type": None,
                "copy_cel": None,
                "error": "type conflict: String | Int64 -- declare data_type explicitly",
            }
        ],
    },
    {
        "id": "unknown_path",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("ghost")],
        "path_types": {},
        "expected": [
            {
                "json_path": "ghost",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "index_type": None,
                "copy_cel": None,
                "error": "path 'ghost' not found in _json",
            }
        ],
    },
    {
        "id": "unknown_index_type",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.email", index_type="zzz")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "index_type": None,
                "copy_cel": None,
                "error": (
                    "unknown index_type 'zzz'. Valid: "
                    "bloom_filter, minmax, ngrambf_v1, set, tokenbf_v1"
                ),
            }
        ],
    },
    {
        "id": "batch_partial",
        "existing": [_JSON_COL],
        "requests": [
            make_promotion_request("user.email"),
            make_promotion_request("ghost"),
        ],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "ok",
                "column_name": "user_email",
                "data_type": "string",
                "index_type": None,
                "copy_cel": "_json.user.email",
                "error": None,
            },
            {
                "json_path": "ghost",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "index_type": None,
                "copy_cel": None,
                "error": "path 'ghost' not found in _json",
            },
        ],
    },
]


class TestBuildPromotionColumns:
    @pytest.mark.parametrize("case", BUILD_CASES, ids=[c["id"] for c in BUILD_CASES])
    def test_outcomes(self, case: BuildCase):
        outcomes = build_promotion_columns(
            case["existing"],
            case["requests"],
            type_registry=TypeRegistry.default(),
            path_types=case["path_types"],
        )
        assert _project(outcomes) == case["expected"]

    def test_ok_column_carries_copy_directive_and_index_use_case(self):
        outcomes = build_promotion_columns(
            [_JSON_COL],
            [make_promotion_request("user.email", index_type="bloom_filter")],
            type_registry=TypeRegistry.default(),
            path_types={"user.email": ["String"]},
        )
        column = outcomes[0].column
        assert column.expr == "@copy: _json.user.email"
        assert column.use_case == "bloom"
        assert column.type == "string"
        assert column.comment == "Promoted from _json.user.email"
        assert column.field_type == "promoted"

    def test_index_use_case_invalid_for_primitive_errors(self):
        # minmax -> range use_case, which is not valid for a string primitive.
        outcomes = build_promotion_columns(
            [_JSON_COL],
            [make_promotion_request("user.email", index_type="minmax")],
            type_registry=TypeRegistry.default(),
            path_types={"user.email": ["String"]},
        )
        assert outcomes[0].status == "error"
        assert "not valid for primitive 'string'" in outcomes[0].error


# ── promotion_preview_ddl ────────────────────────────────────


class TestPromotionPreviewDdl:
    def test_emits_add_column_and_index(self):
        outcomes = build_promotion_columns(
            [_JSON_COL],
            [make_promotion_request("user.email", index_type="bloom_filter")],
            type_registry=TypeRegistry.default(),
            path_types={"user.email": ["String"]},
        )
        columns = [o.column for o in outcomes if o.status == "ok"]
        statements = promotion_preview_ddl("filebeat", columns, db="dfe")
        joined = "\n".join(statements)
        assert "ALTER TABLE dfe.filebeat ADD COLUMN IF NOT EXISTS `user_email`" in joined
        assert "@copy: _json.user.email" in joined
        assert "ADD INDEX idx_user_email `user_email` TYPE bloom_filter" in joined


# ── discover_paths (SQL building) ────────────────────────────


class _RecordingClient:
    """Fake ClickHouse client that records queries and returns canned rows.

    Routes by SQL shape: the discovery GROUP BY query, the sample query
    (``ORDER BY rand()``), and the stats query (``uniqHLL12``).
    """

    def __init__(
        self,
        discover_rows: list[tuple[str, str]] | None = None,
        sample_rows: list[tuple[str]] | None = None,
        stats_rows: list[tuple[float, int]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._discover_rows = discover_rows or []
        self._sample_rows = sample_rows or []
        self._stats_rows = stats_rows or [(100.0, 1)]

    def execute(self, sql: str, parameters: dict[str, Any] | None = None) -> list:
        self.calls.append((sql, parameters or {}))
        if "JSONDynamicPathsWithTypes" in sql:
            return self._discover_rows
        if "uniqHLL12" in sql:
            return self._stats_rows
        if "ORDER BY rand()" in sql:
            return self._sample_rows
        return []


class TestDiscoverPaths:
    def test_basic_discovery_sql_and_result(self):
        client = _RecordingClient(discover_rows=[("user.id", "Int64"), ("user.email", "String")])
        result = discover_paths(client, db="dfe", source="syslog", existing_columns=[])

        sql, params = client.calls[0]
        assert "FROM `dfe`.`syslog`" in sql
        assert "JSONDynamicPathsWithTypes(assumeNotNull(_json))" in sql
        assert "GROUP BY path, type" in sql
        assert "WHERE" not in sql
        assert params == {}

        by_path = {d.path: d for d in result}
        assert by_path["user.id"].types == ["Int64"]
        assert by_path["user.id"].is_consistent is True
        assert by_path["user.id"].column_type == "integer"
        assert by_path["user.email"].suggested_column_name == "user_email"
        assert by_path["user.email"].column_type == "string"
        assert by_path["user.email"].copy_expr == "@copy: _json.user.email"

    def test_json_match_field_targets_subcolumn(self):
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="default",
            existing_columns=[],
            match_field="_json.tags.collector.type",
            match_value="syslog",
        )
        sql, params = client.calls[0]
        assert "FROM `dfe`.`default`" in sql
        assert (
            "WHERE toString(assumeNotNull(_json).`tags.collector.type`) = {match_value:String}"
            in sql
        )
        assert params == {"match_value": "syslog"}

    def test_bare_match_field_targets_real_column(self):
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="default",
            existing_columns=[],
            match_field="_org_id",
            match_value="acme",
        )
        sql, params = client.calls[0]
        assert "WHERE toString(`_org_id`) = {match_value:String}" in sql
        assert "assumeNotNull(_json)" not in sql.split("WHERE", 1)[1]
        assert params == {"match_value": "acme"}

    def test_paths_filter_adds_having(self):
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(client, db="dfe", source="syslog", existing_columns=[], paths=["a", "b"])
        sql, params = client.calls[0]
        assert "HAVING path IN {paths:Array(String)}" in sql
        assert params["paths"] == ["a", "b"]

    def test_samples_query_ands_match_filter(self):
        client = _RecordingClient(discover_rows=[("a", "String")], sample_rows=[("x",), ("y",)])
        result = discover_paths(
            client,
            db="dfe",
            source="default",
            existing_columns=[],
            match_field="_json.f",
            match_value="v",
            samples=2,
        )
        sample_sql, sample_params = client.calls[1]
        assert "ORDER BY rand()" in sample_sql
        assert "AND toString(assumeNotNull(_json).`f`) = {match_value:String}" in sample_sql
        assert sample_params == {"n": 2, "match_value": "v"}
        assert result[0].samples == ["x", "y"]

    def test_stats_query_ands_match_filter(self):
        client = _RecordingClient(discover_rows=[("a", "String")], stats_rows=[(42.5, 7)])
        result = discover_paths(
            client,
            db="dfe",
            source="default",
            existing_columns=[],
            match_field="_json.f",
            match_value="v",
            stats=True,
        )
        stats_sql, stats_params = client.calls[1]
        assert "uniqHLL12" in stats_sql
        assert "WHERE toString(assumeNotNull(_json).`f`) = {match_value:String}" in stats_sql
        assert stats_params == {"match_value": "v"}
        assert result[0].coverage_pct == 42.5
        assert result[0].distinct_count == 7


# ── sample_rows (SQL building) ───────────────────────────────


class _SampleRowsClient:
    """Fake ClickHouse client exposing ``query_rows``; records each call."""

    def __init__(
        self,
        columns: list[str] | None = None,
        rows: list[tuple] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._columns = columns or []
        self._rows = rows or []

    def query_rows(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple]]:
        self.calls.append((sql, parameters or {}))
        return self._columns, self._rows


class TestSampleRows:
    def test_whole_table_sql_and_row_assembly(self):
        client = _SampleRowsClient(
            columns=["_org_id", "_json"],
            rows=[("acme", {"a": 1}), ("acme", {"a": 2})],
        )
        columns, rows = sample_rows(client, db="dfe", source="syslog", limit=5)

        sql, params = client.calls[0]
        assert "SELECT * FROM `dfe`.`syslog`" in sql
        assert "ORDER BY rand() LIMIT {limit:UInt32}" in sql
        assert "WHERE" not in sql
        assert params == {"limit": 5}

        assert columns == ["_org_id", "_json"]
        assert rows == [
            {"_org_id": "acme", "_json": {"a": 1}},
            {"_org_id": "acme", "_json": {"a": 2}},
        ]

    def test_match_filter_ands_into_where(self):
        client = _SampleRowsClient(columns=["_json"], rows=[])
        sample_rows(
            client,
            db="dfe",
            source="default",
            match_field="_json.tags.collector.type",
            match_value="syslog",
            limit=10,
        )
        sql, params = client.calls[0]
        assert "FROM `dfe`.`default`" in sql
        assert (
            "WHERE toString(assumeNotNull(_json).`tags.collector.type`) = {match_value:String}"
            in sql
        )
        assert params == {"limit": 10, "match_value": "syslog"}

    def test_exists_match_operator(self):
        client = _SampleRowsClient(columns=["_json"], rows=[])
        sample_rows(
            client,
            db="dfe",
            source="default",
            match_field="_json.tags.type",
            match_operator="exists",
            limit=3,
        )
        sql, params = client.calls[0]
        assert "isNotNull(assumeNotNull(_json).`tags.type`)" in sql
        assert "notEmpty(toString(assumeNotNull(_json).`tags.type`))" in sql
        assert params == {"limit": 3}

    def test_includes_match_operator(self):
        client = _SampleRowsClient(columns=["_json"], rows=[])
        sample_rows(
            client,
            db="dfe",
            source="default",
            match_field="_json.message",
            match_value="error",
            match_operator="includes",
            limit=5,
        )
        sql, params = client.calls[0]
        assert (
            "positionCaseInsensitive(toString(assumeNotNull(_json).`message`), {match_value:String})"
            in sql
        )
        assert params == {"limit": 5, "match_value": "error"}

    def test_empty_result_still_returns_columns(self):
        client = _SampleRowsClient(columns=["_json"], rows=[])
        columns, rows = sample_rows(client, db="dfe", source="syslog")
        assert columns == ["_json"]
        assert rows == []
