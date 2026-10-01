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
    JsonPromotionError,
    PromotionRequest,
    build_promotion_columns,
    ch_dynamic_type_to_primitive,
    discover_paths,
    json_column_path,
    json_subcolumn,
    list_promoted_json_fields,
    promoted_column_expr,
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


# ── json_column_path / promoted_column_expr / qualified_table ─


class JsonColumnPathCase(TypedDict):
    id: str
    path: str
    expected: str


JSON_COLUMN_PATH_CASES: list[JsonColumnPathCase] = [
    {"id": "nested", "path": "user.email", "expected": "_json.user.email"},
    {"id": "flat", "path": "id", "expected": "_json.id"},
]


class TestJsonColumnPath:
    @pytest.mark.parametrize(
        "case", JSON_COLUMN_PATH_CASES, ids=[c["id"] for c in JSON_COLUMN_PATH_CASES]
    )
    def test_builds(self, case: JsonColumnPathCase):
        assert json_column_path(case["path"]) == case["expected"]


class PromotedExprCase(TypedDict):
    id: str
    path: str
    expected: str


PROMOTED_EXPR_CASES: list[PromotedExprCase] = [
    {"id": "nested", "path": "user.email", "expected": "@source: user.email"},
    {"id": "flat", "path": "id", "expected": "@source: id"},
    {
        "id": "deep_nested_stays_bare",
        "path": "probe.promote.value",
        "expected": "@source: probe.promote.value",
    },
]


class TestPromotedColumnExpr:
    @pytest.mark.parametrize(
        "case", PROMOTED_EXPR_CASES, ids=[c["id"] for c in PROMOTED_EXPR_CASES]
    )
    def test_builds_bare_source_directive(self, case: PromotedExprCase):
        assert promoted_column_expr(case["path"]) == case["expected"]

    def test_never_prefixes_the_json_column(self):
        # The loader reads the arriving record, where the field is not under _json.
        assert "_json" not in promoted_column_expr("user.email")


class QualifiedTableCase(TypedDict):
    id: str
    db: str
    source: str
    expected: str


QUALIFIED_TABLE_CASES: list[QualifiedTableCase] = [
    {"id": "default", "db": "default", "source": "filebeat", "expected": "`default`.`filebeat`"},
    {"id": "custom_db", "db": "dfe", "source": "aws_ct", "expected": "`dfe`.`aws_ct`"},
    {"id": "backtick", "db": "dfe", "source": "a`b", "expected": "`dfe`.`a``b`"},
    {"id": "backslash", "db": "d\\", "source": "t", "expected": "`d\\\\`.`t`"},
]


class TestQualifiedTable:
    @pytest.mark.parametrize(
        "case", QUALIFIED_TABLE_CASES, ids=[c["id"] for c in QUALIFIED_TABLE_CASES]
    )
    def test_quotes(self, case: QualifiedTableCase):
        assert qualified_table(case["db"], case["source"]) == case["expected"]


class TestJsonSubcolumn:
    def test_a_plain_path_is_one_quoted_identifier(self):
        assert json_subcolumn("user.email") == "assumeNotNull(_json).`user.email`"

    def test_a_trailing_backslash_cannot_escape_the_closing_backtick(self):
        assert json_subcolumn("a\\") == "assumeNotNull(_json).`a\\\\`"

    def test_a_backtick_is_refused(self):
        with pytest.raises(JsonPromotionError, match="Illegal JSON path"):
            json_subcolumn("a`b")


# ── promoted_paths ───────────────────────────────────────────


class PromotedPathsCase(TypedDict):
    id: str
    columns: list[MetaSchemaColumn]
    expected: dict[str, str]


def make_promoted_column(name: str, path: str, **kwargs: object) -> MetaSchemaColumn:
    """A promoted meta-schema column, exactly as the promote path writes one."""
    return make_meta_schema_column(
        name=name, expr=promoted_column_expr(path), _field_type="promoted", **kwargs
    )


PROMOTED_PATHS_CASES: list[PromotedPathsCase] = [
    {
        "id": "promoted_column",
        "columns": [make_promoted_column("user_email", "user.email")],
        "expected": {"user.email": "user_email"},
    },
    {
        "id": "flat_path",
        "columns": [make_promoted_column("uid", "user.id")],
        "expected": {"user.id": "uid"},
    },
    {
        # A header column spells its fill rule @source too, so the marker decides.
        "id": "header_source_column_ignored",
        "columns": [make_meta_schema_column(name="foo", expr="@source: foo", _field_type="base")],
        "expected": {},
    },
    {
        "id": "unmarked_source_column_ignored",
        "columns": [make_meta_schema_column(name="foo", expr="@source: foo")],
        "expected": {},
    },
    {
        "id": "no_expr_ignored",
        "columns": [make_meta_schema_column(name="bar", _field_type="promoted")],
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
            make_promoted_column(
                "cloud_trail_event_tls_details_cipher_suite",
                "CloudTrailEvent.tlsDetails.cipherSuite",
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
            "use_case": o.use_case,
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
                "use_case": None,
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
                "use_case": None,
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
                "use_case": None,
                "copy_cel": "_json.user.email",
                "error": None,
            }
        ],
    },
    {
        "id": "with_use_case",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.email", use_case="exact_match")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "ok",
                "column_name": "user_email",
                "data_type": "string",
                "use_case": "exact_match",
                "copy_cel": "_json.user.email",
                "error": None,
            }
        ],
    },
    {
        "id": "already_promoted",
        "existing": [
            _JSON_COL,
            make_promoted_column("user_email", "user.email"),
        ],
        "requests": [make_promotion_request("user.email")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "use_case": None,
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
                "use_case": None,
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
                "use_case": None,
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
                "use_case": None,
                "copy_cel": None,
                "error": "path 'ghost' not found in _json",
            }
        ],
    },
    {
        "id": "unknown_use_case",
        "existing": [_JSON_COL],
        "requests": [make_promotion_request("user.email", use_case="zzz")],
        "path_types": {"user.email": ["String"]},
        "expected": [
            {
                "json_path": "user.email",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "use_case": None,
                "copy_cel": None,
                "error": (
                    "Column 'user_email': Unknown use case 'zzz'. Valid: "
                    "dimension, exact_match, key_search, range, similarity_search, "
                    "substring_search, word_search"
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
                "use_case": None,
                "copy_cel": "_json.user.email",
                "error": None,
            },
            {
                "json_path": "ghost",
                "status": "error",
                "column_name": None,
                "data_type": None,
                "use_case": None,
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

    def test_ok_column_carries_source_directive_and_index_use_case(self):
        outcomes = build_promotion_columns(
            [_JSON_COL],
            [make_promotion_request("user.email", use_case="exact_match")],
            type_registry=TypeRegistry.default(),
            path_types={"user.email": ["String"]},
        )
        column = outcomes[0].column
        assert column.expr == "@source: user.email"
        assert column.use_case == "exact_match"
        assert column.type == "string"
        assert column.comment == "Promoted from _json.user.email"
        assert column.field_type == "promoted"

    @pytest.mark.parametrize(
        ("path", "reason"),
        [
            ("user.email | now()", "fallback separator"),
            ("user - email", "ends the directive"),
            (" user.email", "leading or trailing whitespace"),
        ],
        ids=["pipe", "space_hyphen_space", "whitespace"],
    )
    def test_path_the_loader_could_not_read_back_is_refused(self, path: str, reason: str):
        outcomes = build_promotion_columns(
            [_JSON_COL],
            [make_promotion_request(path, data_type="string")],
            type_registry=TypeRegistry.default(),
            path_types={},
        )
        assert outcomes[0].status == "error"
        assert reason in outcomes[0].error

    def test_index_use_case_invalid_for_primitive_errors(self):
        # range is not a question a string column can be asked.
        outcomes = build_promotion_columns(
            [_JSON_COL],
            [make_promotion_request("user.email", use_case="range")],
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
            [make_promotion_request("user.email", use_case="exact_match")],
            type_registry=TypeRegistry.default(),
            path_types={"user.email": ["String"]},
        )
        columns = [o.column for o in outcomes if o.status == "ok"]
        statements = promotion_preview_ddl("filebeat", columns, db="dfe")
        joined = "\n".join(statements)
        assert "ALTER TABLE `dfe`.`filebeat` ADD COLUMN IF NOT EXISTS `user_email`" in joined
        assert "COMMENT '@source: user.email - Promoted from _json.user.email'" in joined
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
        assert by_path["user.email"].column_expr == "@source: user.email"

    def test_json_match_field_targets_subcolumn(self):
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="main",
            existing_columns=[],
            match_field="_json.tags.collector.type",
            match_value="syslog",
        )
        sql, params = client.calls[0]
        assert "FROM `dfe`.`main`" in sql
        assert (
            "WHERE toString(assumeNotNull(_json).`tags.collector.type`) = {match_value:String}"
            in sql
        )
        assert params == {"match_value": "syslog"}

    def test_bare_header_field_targets_real_column(self):
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="main",
            existing_columns=[],
            match_field="_org_id",
            match_value="acme",
        )
        sql, params = client.calls[0]
        assert "WHERE toString(`_org_id`) = {match_value:String}" in sql
        assert "assumeNotNull(_json)" not in sql.split("WHERE", 1)[1]
        assert params == {"match_value": "acme"}

    def test_a_bare_dotted_path_is_the_payload_path_the_router_reads(self):
        # dfe-engine#335: the receiver splits match.field on '.' and walks the raw
        # payload, so a source written for routing has to discover as well.
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="main",
            existing_columns=[],
            match_field="tags.collector.type",
            match_value="syslog",
        )
        sql, params = client.calls[0]
        assert (
            "WHERE toString(assumeNotNull(_json).`tags.collector.type`) = {match_value:String}"
            in sql
        )
        assert params == {"match_value": "syslog"}

    def test_the_routers_spelling_and_the_json_prefix_resolve_the_same(self):
        bare = _RecordingClient(discover_rows=[("a", "String")])
        prefixed = _RecordingClient(discover_rows=[("a", "String")])
        for client, field in (
            (bare, "tags.collector.type"),
            (prefixed, "_json.tags.collector.type"),
        ):
            discover_paths(
                client,
                db="dfe",
                source="main",
                existing_columns=[],
                match_field=field,
                match_value="syslog",
            )
        assert bare.calls[0] == prefixed.calls[0]

    def test_a_bare_top_level_payload_key_is_a_subcolumn_too(self):
        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="main",
            existing_columns=[],
            match_field="ingest_type",
            match_value="beats",
        )
        sql, _ = client.calls[0]
        assert "WHERE toString(assumeNotNull(_json).`ingest_type`) = {match_value:String}" in sql

    def test_one_nested_match_field_works_for_routing_and_for_discovery(self):
        # The same string through both readers: the receiver rule the routing
        # compiles, and the SQL the discovery endpoint builds.
        from dfe_engine.services.source_routing import receiver_match
        from dfe_engine.source.models import Source, SourceMatch

        source = Source(
            source="filebeat",
            match=SourceMatch(field="tags.collector.type", value="syslog"),
        )
        rule = receiver_match(source)
        assert rule is not None
        assert rule.field == "tags.collector.type"

        client = _RecordingClient(discover_rows=[("a", "String")])
        discover_paths(
            client,
            db="dfe",
            source="main",
            existing_columns=[],
            match_field=rule.field,
            match_value=rule.value,
        )
        sql, _ = client.calls[0]
        assert "assumeNotNull(_json).`tags.collector.type`" in sql

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
            source="main",
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
            source="main",
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
            source="main",
            match_field="_json.tags.collector.type",
            match_value="syslog",
            limit=10,
        )
        sql, params = client.calls[0]
        assert "FROM `dfe`.`main`" in sql
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
            source="main",
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
            source="main",
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
