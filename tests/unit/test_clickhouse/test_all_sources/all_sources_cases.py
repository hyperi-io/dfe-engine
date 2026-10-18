#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_all_sources/all_sources_cases.py
#  Purpose:      Case tables for the all_sources Merge table and its HyperDX source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the all_sources Merge table and its HyperDX source."""

from typing import TypedDict

from dfe_engine.schema.applier import LiveTable
from dfe_engine.source.models import Source
from tests.unit.test_clickhouse.factories import make_live_table, make_source

# The landing table's header columns as system.columns reports them for the timeseries profile at 1.0.2.
LANDING_COLUMNS = {
    "_timestamp_load": "DateTime64(3, 'UTC')",
    "_timestamp": "DateTime64(3, 'UTC')",
    "_timestamp_received": "DateTime64(3, 'UTC')",
    "_org_id": "LowCardinality(String)",
    "_source": "LowCardinality(Nullable(String))",
    "_raw": "Nullable(String)",
    "_json": "JSON(max_dynamic_paths=2048)",
}

MERGE_COLUMNS = [
    ("_timestamp_load", "DateTime64(3, 'UTC')"),
    ("_timestamp", "DateTime64(3, 'UTC')"),
    ("_timestamp_received", "DateTime64(3, 'UTC')"),
    ("_org_id", "LowCardinality(String)"),
    ("_source", "LowCardinality(Nullable(String))"),
    ("_raw", "Nullable(String)"),
    ("_json", "JSON(max_dynamic_paths=2048)"),
]

# Tables the manifest owns in the data database, which a source of the same name must not pull into all_sources.
MANIFEST_TABLES = frozenset({"detection", "main", "otel_logs", "repository"})

# The members the regex cases select from: one bare name and one with a hyphen.
SELECTS_MEMBERS = ["main", "zeek-conn"]


class MemberPatternCase(TypedDict):
    id: str
    expected_pattern: str
    members: list[str]


class MemberPatternSelectsCase(TypedDict):
    id: str
    expected_selected: bool
    table: str


class MemberTablesCase(TypedDict):
    id: str
    expected_members: list[str]
    landing_table: str
    live: dict[str, LiveTable]
    reserved: frozenset[str]
    sources: list[Source]
    with_org_id: frozenset[str]


class MemberTablesRejectsCase(TypedDict):
    id: str
    landing_table: str
    message: str


class MergeColumnsCase(TypedDict):
    id: str
    expected_columns: list[tuple[str, str]]
    landing_columns: dict[str, str]


class MergeColumnsRejectsCase(TypedDict):
    id: str
    landing_columns: dict[str, str]
    message: str


class MergeTableDdlCase(TypedDict):
    id: str
    columns: list[tuple[str, str]]
    database: str
    expected_ddl: str
    members: list[str]


MEMBER_PATTERN_CASES: list[MemberPatternCase] = [
    {"id": "single_member", "expected_pattern": "^(main)$", "members": ["main"]},
    {
        "id": "hyphenated_member",
        "expected_pattern": "^(main|zeek-conn)$",
        "members": ["main", "zeek-conn"],
    },
    {
        "id": "underscored_member",
        "expected_pattern": "^(main|web_logs)$",
        "members": ["main", "web_logs"],
    },
]

MEMBER_PATTERN_SELECTS_CASES: list[MemberPatternSelectsCase] = [
    {"id": "landing_table", "expected_selected": True, "table": "main"},
    {"id": "hyphenated_member", "expected_selected": True, "table": "zeek-conn"},
    {
        "id": "per_source_view_on_the_landing_table",
        "expected_selected": False,
        "table": "main_sigma",
    },
    {"id": "prefix_of_a_member", "expected_selected": False, "table": "xmain"},
    {"id": "suffix_of_a_member", "expected_selected": False, "table": "mainx"},
    {"id": "first_half_of_a_hyphenated_member", "expected_selected": False, "table": "zeek"},
    {"id": "second_half_of_a_hyphenated_member", "expected_selected": False, "table": "conn"},
    {"id": "longer_hyphenated_name", "expected_selected": False, "table": "zeek-conn-old"},
    {"id": "hyphen_is_not_a_range", "expected_selected": False, "table": "zeekXconn"},
    {"id": "the_alternation_as_a_name", "expected_selected": False, "table": "main|zeek-conn"},
    {"id": "the_merge_table_itself", "expected_selected": False, "table": "all_sources"},
]

MEMBER_TABLES_CASES: list[MemberTablesCase] = [
    {
        "id": "landing_only",
        "expected_members": ["main"],
        "landing_table": "main",
        "live": {"main": make_live_table(engine="MergeTree")},
        "reserved": MANIFEST_TABLES,
        "sources": [],
        "with_org_id": frozenset({"main"}),
    },
    {
        "id": "source_with_its_own_table",
        "expected_members": ["main", "zeek"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "zeek": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="zeek")],
        "with_org_id": frozenset({"main", "zeek"}),
    },
    {
        "id": "source_not_yet_deployed",
        "expected_members": ["main"],
        "landing_table": "main",
        "live": {"main": make_live_table(engine="MergeTree")},
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="zeek")],
        "with_org_id": frozenset({"main"}),
    },
    {
        "id": "views_are_not_members",
        "expected_members": ["main"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "okta": make_live_table(engine="MaterializedView"),
            "zeek": make_live_table(engine="View"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="okta"), make_source(name="zeek")],
        "with_org_id": frozenset({"main", "okta", "zeek"}),
    },
    {
        "id": "core_tables_outside_the_registry",
        "expected_members": ["main"],
        "landing_table": "main",
        "live": {
            "detection": make_live_table(engine="ReplacingMergeTree"),
            "main": make_live_table(engine="MergeTree"),
            "meta": make_live_table(engine="MergeTree"),
            "otel_logs": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [],
        "with_org_id": frozenset({"detection", "main", "meta", "otel_logs"}),
    },
    {
        "id": "the_merge_table_itself",
        "expected_members": ["main"],
        "landing_table": "main",
        "live": {
            "all_sources": make_live_table(engine="Merge"),
            "main": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [],
        "with_org_id": frozenset({"all_sources", "main"}),
    },
    {
        "id": "a_landing_table_named_all_sources",
        "expected_members": [],
        "landing_table": "all_sources",
        "live": {"all_sources": make_live_table(engine="MergeTree")},
        "reserved": MANIFEST_TABLES,
        "sources": [],
        "with_org_id": frozenset({"all_sources"}),
    },
    {
        "id": "deleted_source_whose_table_remains",
        "expected_members": ["main", "zeek"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "okta": make_live_table(engine="MergeTree"),
            "zeek": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="zeek")],
        "with_org_id": frozenset({"main", "okta", "zeek"}),
    },
    {
        "id": "replicated_and_shared_engines",
        "expected_members": ["main", "okta", "zeek"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="ReplicatedMergeTree"),
            "okta": make_live_table(engine="ReplicatedReplacingMergeTree"),
            "zeek": make_live_table(engine="SharedReplacingMergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="okta"), make_source(name="zeek")],
        "with_org_id": frozenset({"main", "okta", "zeek"}),
    },
    {
        "id": "hyphenated_source_table",
        "expected_members": ["main", "zeek-conn"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "zeek-conn": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="zeek-conn")],
        "with_org_id": frozenset({"main", "zeek-conn"}),
    },
    {
        "id": "sorted_with_the_landing_source_listed_once",
        "expected_members": ["apache", "main", "zeek"],
        "landing_table": "main",
        "live": {
            "apache": make_live_table(engine="MergeTree"),
            "main": make_live_table(engine="MergeTree"),
            "zeek": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [
            make_source(name="zeek"),
            make_source(name="main"),
            make_source(name="apache"),
        ],
        "with_org_id": frozenset({"apache", "main", "zeek"}),
    },
    {
        "id": "configured_landing_table",
        "expected_members": ["landing"],
        "landing_table": "landing",
        "live": {
            "landing": make_live_table(engine="MergeTree"),
            "main": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [],
        "with_org_id": frozenset({"landing", "main"}),
    },
    {
        "id": "source_named_after_a_manifest_table",
        "expected_members": ["main"],
        "landing_table": "main",
        "live": {
            "detection": make_live_table(engine="ReplacingMergeTree"),
            "main": make_live_table(engine="MergeTree"),
            "repository": make_live_table(engine="ReplacingMergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="detection"), make_source(name="repository")],
        "with_org_id": frozenset({"detection", "main", "repository"}),
    },
    {
        "id": "landing_source_is_the_one_reserved_member",
        "expected_members": ["main", "zeek"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "zeek": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="main"), make_source(name="zeek")],
        "with_org_id": frozenset({"main", "zeek"}),
    },
    {
        "id": "member_without_org_id",
        "expected_members": ["main", "zeek"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "noorg": make_live_table(engine="MergeTree"),
            "zeek": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="noorg"), make_source(name="zeek")],
        "with_org_id": frozenset({"main", "zeek"}),
    },
    {
        "id": "landing_table_without_org_id",
        "expected_members": ["zeek"],
        "landing_table": "main",
        "live": {
            "main": make_live_table(engine="MergeTree"),
            "zeek": make_live_table(engine="MergeTree"),
        },
        "reserved": MANIFEST_TABLES,
        "sources": [make_source(name="zeek")],
        "with_org_id": frozenset({"zeek"}),
    },
]

MEMBER_TABLES_REJECTS_CASES: list[MemberTablesRejectsCase] = [
    {
        "id": "dotted",
        "landing_table": "dfe.main",
        "message": "table name 'dfe.main' must be one or more letters, digits, '_' or '-', so all_sources can list it by regex",
    },
    {
        "id": "statement_separator",
        "landing_table": "main;drop",
        "message": "table name 'main;drop' must be one or more letters, digits, '_' or '-', so all_sources can list it by regex",
    },
    {
        "id": "regex_alternation",
        "landing_table": "main|.*",
        "message": "table name 'main|.*' must be one or more letters, digits, '_' or '-', so all_sources can list it by regex",
    },
    {
        "id": "empty",
        "landing_table": "",
        "message": "table name '' must be one or more letters, digits, '_' or '-', so all_sources can list it by regex",
    },
]

MERGE_COLUMNS_CASES: list[MergeColumnsCase] = [
    {
        "id": "header_columns_in_declared_order",
        "expected_columns": MERGE_COLUMNS,
        "landing_columns": LANDING_COLUMNS,
    },
    {
        "id": "landing_order_does_not_matter",
        "expected_columns": MERGE_COLUMNS,
        "landing_columns": dict(reversed(LANDING_COLUMNS.items())),
    },
    {
        "id": "columns_outside_the_header_are_left_out",
        "expected_columns": MERGE_COLUMNS,
        "landing_columns": {
            **LANDING_COLUMNS,
            "_tags": "JSON",
            "_uuid": "Nullable(UUID)",
        },
    },
]

MERGE_COLUMNS_REJECTS_CASES: list[MergeColumnsRejectsCase] = [
    {
        "id": "landing_table_without_raw",
        "landing_columns": {name: kind for name, kind in LANDING_COLUMNS.items() if name != "_raw"},
        "message": "landing table 'main' lacks the columns ['_raw'], which all_sources reads",
    },
    {
        "id": "landing_table_absent",
        "landing_columns": {},
        "message": (
            "landing table 'main' lacks the columns ['_timestamp_load', '_timestamp', "
            "'_timestamp_received', '_org_id', '_source', '_raw', '_json'], which all_sources reads"
        ),
    },
]

MERGE_TABLE_DDL_CASES: list[MergeTableDdlCase] = [
    {
        "id": "single_member",
        "columns": [("_timestamp_load", "DateTime64(3)"), ("_json", "JSON")],
        "database": "dfe",
        "expected_ddl": (
            "CREATE OR REPLACE TABLE `dfe`.`all_sources` "
            "(`_timestamp_load` DateTime64(3), `_json` JSON) "
            "ENGINE = Merge(`dfe`, '^(main)$')"
        ),
        "members": ["main"],
    },
    {
        # The Replicated database replicates the CREATE, and refuses ON CLUSTER inside it.
        "id": "two_members_and_no_on_cluster",
        "columns": [("_timestamp_load", "DateTime64(3)"), ("_json", "JSON")],
        "database": "dfe",
        "expected_ddl": (
            "CREATE OR REPLACE TABLE `dfe`.`all_sources` "
            "(`_timestamp_load` DateTime64(3), `_json` JSON) "
            "ENGINE = Merge(`dfe`, '^(main|zeek-conn)$')"
        ),
        "members": ["main", "zeek-conn"],
    },
    {
        "id": "every_header_column",
        "columns": MERGE_COLUMNS,
        "database": "dfe",
        "expected_ddl": (
            "CREATE OR REPLACE TABLE `dfe`.`all_sources` ("
            "`_timestamp_load` DateTime64(3, 'UTC'), `_timestamp` DateTime64(3, 'UTC'), "
            "`_timestamp_received` DateTime64(3, 'UTC'), `_org_id` LowCardinality(String), "
            "`_source` LowCardinality(Nullable(String)), `_raw` Nullable(String), "
            "`_json` JSON(max_dynamic_paths=2048)) "
            "ENGINE = Merge(`dfe`, '^(main|zeek)$')"
        ),
        "members": ["main", "zeek"],
    },
    {
        "id": "hyphenated_database",
        "columns": [("_timestamp_load", "DateTime64(3)")],
        "database": "dfe-prod",
        "expected_ddl": (
            "CREATE OR REPLACE TABLE `dfe-prod`.`all_sources` "
            "(`_timestamp_load` DateTime64(3)) "
            "ENGINE = Merge(`dfe-prod`, '^(main)$')"
        ),
        "members": ["main"],
    },
]
