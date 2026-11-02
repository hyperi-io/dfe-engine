#  Project:      dfe-engine
#  File:         tests/integration/test_all_sources_ch.py
#  Purpose:      Prove all_sources reads every source table and fences each tenant, on a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Real-ClickHouse proof that all_sources reads the landing table and each deployed source's table and fences every tenant to its own rows.

The source tables come from the DDL a deploy generates for the minimal and passthrough header profiles, which lack header columns the landing table has. Every assertion reads what the server returns.
"""

import calendar
import functools
import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, TypedDict

import clickhouse_connect
import pytest
from clickhouse_connect.driver.exceptions import DatabaseError
from dfe_schemas.clickhouse import PlatformProbe

from dfe_engine.clickhouse.all_sources import (
    ALL_SOURCES,
    merge_columns,
    merge_table_ddl,
    rebuild_all_sources,
)
from dfe_engine.clickhouse.bootstrap import bootstrap_clickhouse
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.governance.ch.models import TENANT_SETTING, ChTier, tenant_policy_name
from dfe_engine.governance.ch.reconciler import fence_tables
from dfe_engine.governance.ch.render import render_pinned_user, render_tier
from dfe_engine.hyperdx.dashboards import dashboard_files
from dfe_engine.schema.phase import SchemaBootstrapState
from dfe_engine.schema.schema_builder_v2 import SchemaBuilderV2
from dfe_engine.source.deployment import deploy_statements_for_build
from dfe_engine.source.models import Source
from tests.integration.conftest import _unavailable
from tests.support.clickhouse_cleanup import drop_database_and_policies
from tests.support.engine_stack import bootstrap_settings, deploy_source

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

LANDING = "main"
MINIMAL = "allsrc-minimal"
PASSTHROUGH = "allsrc-passthrough"
DEPLOYED = "allsrc-deployed"
BULK_A = "allsrc-bulk-a"
BULK_B = "allsrc-bulk-b"
NO_ORG = "allsrc-noorg"
TYPED = "allsrc-typed"
DETECTION = "detection"

ORG_A = "orga"
ORG_B = "orgb"
UNRESTRICTED = "unrestricted"

# The org ids each viewer's ClickHouse user is pinned to; an empty list is the unrestricted platform shape.
VIEWER_ORGS = {ORG_A: [ORG_A], ORG_B: [ORG_B], UNRESTRICTED: []}

# The header profile each deployed source is built on.
SOURCE_HEADERS = {MINIMAL: "minimal", PASSTHROUGH: "passthrough"}

# Rows inserted per member and org, each count distinct so a swapped table or org changes a total.
ROW_COUNTS = {
    LANDING: {ORG_A: 2, ORG_B: 1},
    MINIMAL: {ORG_A: 3, ORG_B: 4},
    PASSTHROUGH: {ORG_A: 5, ORG_B: 6},
}

# Each member's insert, timed from an anchor so the lag, skew and freshness each have one exact value.
INSERTS = {
    LANDING: (
        "INSERT INTO `{database}`.`{table}` (_timestamp_load, _timestamp, _timestamp_received, _org_id, _source, _raw, _json) "
        "SELECT anchor + INTERVAL 2 SECOND, anchor - INTERVAL 5 SECOND, anchor, {{org:String}}, 'syslog', 'raw-event', '{{}}' "
        "FROM (SELECT fromUnixTimestamp64Milli({{anchor_ms:Int64}}, 'UTC') AS anchor, number FROM numbers({{rows:UInt32}}))"
    ),
    MINIMAL: (
        "INSERT INTO `{database}`.`{table}` (_timestamp_load, _timestamp, _org_id, _json) "
        "SELECT anchor + INTERVAL 12 SECOND, anchor + INTERVAL 10 SECOND, {{org:String}}, '{{}}' "
        "FROM (SELECT fromUnixTimestamp64Milli({{anchor_ms:Int64}}, 'UTC') AS anchor, number FROM numbers({{rows:UInt32}}))"
    ),
    PASSTHROUGH: (
        "INSERT INTO `{database}`.`{table}` (_timestamp_load, _org_id, _json) "
        "SELECT anchor + INTERVAL 20 SECOND, {{org:String}}, '{{}}' "
        "FROM (SELECT fromUnixTimestamp64Milli({{anchor_ms:Int64}}, 'UTC') AS anchor, number FROM numbers({{rows:UInt32}}))"
    ),
}

# A source table with no `_org_id`, which the whole-database fence gives a deny policy.
NO_ORG_TABLE = "CREATE TABLE `{database}`.`{table}` (_timestamp_load DateTime64(3, 'UTC') DEFAULT now64(3), _json JSON) ENGINE = MergeTree ORDER BY _timestamp_load"

# A source table whose header columns carry types other than main's: no timezone, no Nullable, no LowCardinality.
TYPED_TABLE = (
    "CREATE TABLE `{database}`.`{table}` (_timestamp_load DateTime64(3, 'UTC') DEFAULT now64(3), _timestamp DateTime64(3), "
    "_timestamp_received DateTime64(3), _org_id String, _source String, _raw String, _json JSON) "
    "ENGINE = MergeTree ORDER BY _timestamp_load"
)
TYPED_INSERT = (
    "INSERT INTO `{database}`.`{table}` (_timestamp_load, _timestamp, _timestamp_received, _org_id, _source, _raw, _json) "
    "SELECT anchor + INTERVAL 3 SECOND, anchor, anchor + INTERVAL 1 SECOND, {{org:String}}, 'typed', 'typed-raw', '{{}}' "
    "FROM (SELECT fromUnixTimestamp64Milli({{anchor_ms:Int64}}, 'UTC') AS anchor, number FROM numbers(2))"
)
TYPED_ROWS = (
    "SELECT _table, _timestamp, _timestamp_received, _org_id, _source, _raw, length(toString(_json)) "
    "FROM `{database}`.`all_sources` WHERE _table = {{table:String}}"
)

# Objects whose names extend a member's name, each holding rows an unanchored member regex would read.
LOOKALIKES = (
    "CREATE VIEW `{database}`.`main_sigma` AS SELECT * FROM `{database}`.`main`",
    "CREATE TABLE `{database}`.`allsrc-minimal-old` AS `{database}`.`allsrc-minimal`",
    "INSERT INTO `{database}`.`allsrc-minimal-old` SELECT * FROM `{database}`.`allsrc-minimal`",
    "CREATE TABLE `{database}`.`xmain` AS `{database}`.`main`",
    "INSERT INTO `{database}`.`xmain` SELECT * FROM `{database}`.`main`",
)
LOOKALIKE_ROWS = (
    "SELECT (SELECT count() FROM `{database}`.`main_sigma`), (SELECT count() FROM `{database}`.`allsrc-minimal-old`), "
    "(SELECT count() FROM `{database}`.`xmain`)"
)

ROWS_BY_TABLE = (
    "SELECT _table, count() FROM `{database}`.`all_sources` GROUP BY _table ORDER BY _table"
)
ROWS_BY_TABLE_AND_ORG = "SELECT _table, _org_id, count() FROM `{database}`.`all_sources` GROUP BY _table, _org_id ORDER BY _table, _org_id"
ROWS_OF_TABLE = "SELECT count() FROM `{database}`.`all_sources` WHERE _table = '{table}'"
DEFAULT_FILL = (
    "SELECT _table AS table, count() AS rows, "
    "countIf(toUnixTimestamp64Milli(_timestamp) = 0) AS timestamp_at_epoch, "
    "countIf(toUnixTimestamp64Milli(_timestamp_received) = 0) AS received_at_epoch, "
    "countIf(_source IS NULL) AS source_null "
    "FROM `{database}`.`all_sources` GROUP BY _table ORDER BY _table"
)
# Rows by table that an IS NULL or IS NOT NULL filter on `_raw` keeps, with optional query settings appended.
RAW_NULL_ROWS = "SELECT _table, count() FROM `{database}`.`all_sources` WHERE _raw IS NULL GROUP BY _table ORDER BY _table{settings}"
RAW_NOT_NULL_ROWS = "SELECT _table, count() FROM `{database}`.`all_sources` WHERE _raw IS NOT NULL GROUP BY _table ORDER BY _table{settings}"
NO_SUBCOLUMN_REWRITE = " SETTINGS optimize_functions_to_subcolumns = 0"

# What all_sources's ENGINE reads back as in system.tables, with the member regex captured.
MERGE_ENGINE = re.compile(r"Merge\('[^']*', '(?P<pattern>.*)'\)")

# The aggregate each HyperDX aggFn wraps a tile's value expression in.
AGGREGATES = {
    "count": "count()",
    "count_distinct": "count(DISTINCT {expression})",
    "max": "max({expression})",
    "none": "{expression}",
    "quantile": "quantile({level})({expression})",
    "sum": "sum({expression})",
}

# The throughput tiles whose maths the member defaults could skew.
DASHBOARD_TILES = (
    "active-sources",
    "bytes-total",
    "event-skew",
    "events-by-source",
    "lag-max",
    "lag-p95",
    "lag-percentiles",
    "source-freshness",
)

# A lag that reached back to an epoch-0 `_timestamp_received` would be about 56 years.
FIFTY_YEARS_MS = 50 * 365 * 24 * 3600 * 1000

MEMBERS_EXPECTED = [MINIMAL, PASSTHROUGH, LANDING]

ROWS_BY_TABLE_EXPECTED = [(MINIMAL, 7), (PASSTHROUGH, 11), (LANDING, 3)]

DEFAULT_FILL_EXPECTED = {
    MINIMAL: {
        "received_at_epoch": 7,
        "rows": 7,
        "source_null": 7,
        "timestamp_at_epoch": 0,
    },
    PASSTHROUGH: {
        "received_at_epoch": 11,
        "rows": 11,
        "source_null": 11,
        "timestamp_at_epoch": 11,
    },
    LANDING: {
        "received_at_epoch": 0,
        "rows": 3,
        "source_null": 0,
        "timestamp_at_epoch": 0,
    },
}

TENANT_ROWS_EXPECTED = {
    ORG_A: [(MINIMAL, ORG_A, 3), (PASSTHROUGH, ORG_A, 5), (LANDING, ORG_A, 2)],
    ORG_B: [(MINIMAL, ORG_B, 4), (PASSTHROUGH, ORG_B, 6), (LANDING, ORG_B, 1)],
    UNRESTRICTED: [
        (MINIMAL, ORG_A, 3),
        (MINIMAL, ORG_B, 4),
        (PASSTHROUGH, ORG_A, 5),
        (PASSTHROUGH, ORG_B, 6),
        (LANDING, ORG_A, 2),
        (LANDING, ORG_B, 1),
    ],
}

# Times are milliseconds after the insert anchor, `syslog` is the landing rows' own `_source` and the bytes are 3 nine-byte `_raw` values plus 18 two-byte `{}` payloads.
DASHBOARD_EXPECTED = {
    "active-sources": [(3,)],
    "bytes-total": [(63,)],
    "event-skew": [(5, 3)],
    "events-by-source": [(MINIMAL, 7), (PASSTHROUGH, 11), ("syslog", 3)],
    "lag-max": [(2000,)],
    "lag-p95": [(2000.0,)],
    "lag-percentiles": [(2000.0, 2000.0, 2000.0)],
    "source-freshness": [(MINIMAL, 10000, 7), (PASSTHROUGH, 20000, 11), ("syslog", -5000, 3)],
}


class BootstrapLoaderCase(TypedDict):
    """A registry loader handed to a converging bootstrap with what all_sources is afterwards."""

    id: str
    load_sources: Callable[[], Iterable[Source]] | None
    expected: dict[str, object]


BOOTSTRAP_LOADER_CASES: list[BootstrapLoaderCase] = [
    {
        "id": "no_loader",
        "load_sources": None,
        "expected": {"all_sources policies": 0, "member pattern": None, "state": "converged"},
    },
    {
        "id": "empty_registry",
        "load_sources": tuple,
        "expected": {"all_sources policies": 1, "member pattern": "^(main)$", "state": "converged"},
    },
]


@dataclass(frozen=True)
class Estate:
    """A data database with the landing table and two deployed source tables, fenced as a deploy leaves them, their rows and a ClickHouse login per viewer."""

    anchor_ms: int
    database: str
    logins: dict[str, tuple[str, str]]
    params: dict[str, Any]


def _add_member_without_org_id(*, client: Any, estate: Estate) -> None:
    """Give *estate* a source table with no `_org_id` holding 4 rows, fenced as a deploy fences it."""
    database = estate.database
    client.command(NO_ORG_TABLE.format(database=database, table=NO_ORG))
    client.command(f"INSERT INTO `{database}`.`{NO_ORG}` (_json) SELECT '{{}}' FROM numbers(4)")
    # The deploy path's whole-database fence is what gives a table without `_org_id` its deny policy.
    fence_tables(admin_client=client, database=database)


def _aggregate(*, item: dict[str, Any]) -> str:
    """The SQL a tile's select item computes, from its aggFn and value expression."""
    return AGGREGATES[item["aggFn"]].format(
        expression=item["valueExpression"], level=item.get("level")
    )


def _all_sources_state(*, client: Any, database: str) -> dict[str, object]:
    """How many row policies all_sources holds and the member regex it reads through."""
    return {
        "all_sources policies": len(_policies(client=client, database=database, table=ALL_SOURCES)),
        "member pattern": _member_pattern(client=client, database=database),
    }


def _database_policies(*, client: Any, database: str) -> set[str]:
    """The names of every row policy on a table in *database*."""
    rows = client.query(
        "SELECT short_name FROM system.row_policies WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    return {str(row[0]) for row in rows}


def _deploy(*, client: Any, database: str, source: Source) -> None:
    """Run the DDL a deploy of *source* generates against *database*."""
    builder = SchemaBuilderV2(platform=PlatformProbe(client).platform)
    result = builder.build(source=source)
    assert not (result.validation_errors), result.validation_errors
    statements, _ = deploy_statements_for_build(
        builder=builder,
        ch_client=client,
        db=database,
        result=result,
        source=source,
        version_id=source.runtime_version_id(),
    )
    for statement in statements:
        client.command(statement)


def _drop_policy(*, client: Any, database: str, table: str) -> None:
    """Drop the tenant row policy on ``database.table``, failing when there is none."""
    client.command(
        f"DROP ROW POLICY `{tenant_policy_name(db=database, table=table)}` ON `{database}`.`{table}`"
    )


def _drop_quietly(*, client: Any, statement: str) -> None:
    """Run a DROP, ignoring failure so teardown carries on with the rest."""
    try:
        client.command(statement)
    except Exception:
        pass


def _group_by(*, config: dict[str, Any]) -> list[str]:
    """A tile's group-by expressions, from either the string or the list form."""
    group_by = config.get("groupBy") or []
    if isinstance(group_by, str):
        return [group_by]
    return [entry["valueExpression"] for entry in group_by]


def _member_pattern(*, client: Any, database: str) -> str | None:
    """The table regex all_sources reads its members through; None when *database* has no all_sources."""
    rows = client.query(
        "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {tbl:String}",
        parameters={"db": database, "tbl": ALL_SOURCES},
    ).result_rows
    if not (rows):
        return None
    match = MERGE_ENGINE.fullmatch(string=str(rows[0][0]))
    assert match is not None, rows[0][0]
    return match.group("pattern")


def _millis(*, value: datetime) -> int:
    """Milliseconds since the epoch for a ClickHouse UTC timestamp."""
    return calendar.timegm(value.utctimetuple()) * 1000 + value.microsecond // 1000


def _policies(*, client: Any, database: str, table: str) -> list[str]:
    """The row policies ClickHouse holds on ``database.table``."""
    rows = client.query(
        "SELECT short_name FROM system.row_policies WHERE database = {db:String} AND table = {tbl:String} ORDER BY short_name",
        parameters={"db": database, "tbl": table},
    ).result_rows
    return [str(row[0]) for row in rows]


def _policy_filters(*, client: Any, database: str, table: str) -> list[str]:
    """The SELECT filter of each row policy on ``database.table``."""
    rows = client.query(
        "SELECT select_filter FROM system.row_policies WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": database, "tbl": table},
    ).result_rows
    return [str(row[0]) for row in rows]


def _reads(*, estate: Estate, sql: str) -> dict[str, list[tuple]]:
    """What *sql* returns to each viewer."""
    return {viewer: _rows_as(estate=estate, sql=sql, viewer=viewer) for viewer in VIEWER_ORGS}


def _rebuild(*, client: Any, database: str, names: list[str]) -> list[str] | None:
    """Rebuild all_sources the way production does, with a registry holding a source named for each of *names*."""
    sources = [
        make_source(header=SOURCE_HEADERS.get(name, "timeseries"), name=name) for name in names
    ]
    return rebuild_all_sources(
        client=client,
        database=database,
        load_sources=functools.partial(list, sources),
    )


def _relative(*, anchor_ms: int, row: tuple) -> tuple:
    """*row* with each timestamp as milliseconds after the anchor."""

    def shift(value: object) -> object:
        """*value* as milliseconds after the anchor when it is a timestamp, else unchanged."""
        if isinstance(value, datetime):
            return _millis(value=value) - anchor_ms
        return value

    return tuple(shift(value) for value in row)


def _replace_without_fence(*, client: Any, database: str, members: list[str]) -> None:
    """CREATE OR REPLACE all_sources with the statement a rebuild runs, but no fence after it."""
    rows = client.query(
        "SELECT name, type FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": database, "tbl": LANDING},
    ).result_rows
    columns = merge_columns(
        landing_columns={str(name): str(ch_type) for name, ch_type in rows}, landing_table=LANDING
    )
    client.command(merge_table_ddl(columns=columns, database=database, members=members))


def _rows(*, client: Any, parameters: dict[str, Any] | None = None, sql: str) -> list[tuple]:
    """The rows *sql* returns on *client*, each as a tuple."""
    return [tuple(row) for row in client.query(sql, parameters=parameters).result_rows]


def _rows_as(*, estate: Estate, sql: str, viewer: str) -> list[tuple]:
    """The rows *sql* returns to *viewer*'s ClickHouse user."""
    user, password = estate.logins[viewer]
    client = clickhouse_connect.get_client(
        host=estate.params["host"],
        password=password,
        port=estate.params["port"],
        secure=estate.params["secure"],
        username=user,
    )
    try:
        return _rows(client=client, sql=sql)
    finally:
        client.close()


def _tile_sql(*, database: str, tile: dict[str, Any]) -> str:
    """Plain SQL over all_sources computing what a dashboard tile's config asks for."""
    config = tile["config"]
    if config["where"] and config["whereLanguage"] != "sql":
        raise ValueError(
            f"tile {tile['id']!r} filters in {config['whereLanguage']!r}, which this test does not translate"
        )
    if config["displayType"] == "heatmap":
        item = config["select"][0]
        groups = [item["valueExpression"]]
        columns = [item["valueExpression"], item["countExpression"]]
    else:
        groups = _group_by(config=config)
        columns = [*groups, *(_aggregate(item=item) for item in config["select"])]
    sql = f"SELECT {', '.join(columns)} FROM `{database}`.`{ALL_SOURCES}` WHERE {config['where'] or '1'}"
    if groups:
        sql += f" GROUP BY {', '.join(groups)} ORDER BY {', '.join(groups)}"
    return sql


def _tiles() -> dict[str, dict[str, Any]]:
    """The shipped throughput dashboard's tiles by id."""
    return {
        tile["id"]: tile for tile in json.loads(dashboard_files()["dfe-throughput.json"])["tiles"]
    }


def make_source(*, header: str, name: str) -> Source:
    """Build a source.models.Source on *header*'s current profile version that matches records by collector type."""
    return Source.model_validate(
        {
            "header": {"type": header, "version": "1.0.1"},
            "match": {"field": "tags.collector.type", "value": name},
            "schema": {"engine": "", "ttl_days": 90},
            "source": name,
        }
    )


@pytest.fixture
def bootstrap_pass(
    ch_client: Any, ch_params: dict[str, Any]
) -> Iterator[Callable[..., SchemaBootstrapState]]:
    """Run bootstrap_clickhouse against the harness's ClickHouse, dropping each database it ran on with the row policies it left there."""

    def run(
        *,
        bootstrap_tables: bool,
        database: str,
        load_sources: Callable[[], Iterable[Source]] | None,
    ) -> SchemaBootstrapState:
        """One bootstrap pass over *database* with *load_sources* as the registry's loader."""
        databases.append(database)
        ClickHouseManager.reset_instance()
        settings = bootstrap_settings(
            bootstrap_tables=bootstrap_tables, ch_params=ch_params, database=database
        )
        return bootstrap_clickhouse(load_sources=load_sources, settings=settings)

    databases = []
    try:
        yield run
    finally:
        ClickHouseManager.reset_instance()
        for database in databases:
            drop_database_and_policies(client=ch_client, database=database)


@pytest.fixture
def estate(ch_client: Any, ch_params: dict[str, Any], dfe_db: str) -> Iterator[Estate]:
    """The landing table and two deployed source tables holding rows for two orgs, with a pinned user per viewer."""
    try:
        ch_client.command(f"SET {TENANT_SETTING} = 'probe'")
    except Exception as exc:
        _unavailable(reason=f"server does not allow the {TENANT_SETTING!r} custom setting: {exc}")

    uid = uuid.uuid4().hex[:10]
    anchor_ms = (int(time.time()) - 3600) * 1000
    tier = ChTier(grants=[f"SELECT ON `{dfe_db}`.*"], kind="analyst", name=f"allsrc{uid}")
    logins = {
        viewer: (f"dfe_test_allsrc_{viewer}_{uid}", uuid.uuid4().hex) for viewer in VIEWER_ORGS
    }

    try:
        for name, header in SOURCE_HEADERS.items():
            _deploy(client=ch_client, database=dfe_db, source=make_source(header=header, name=name))
        for table, counts in ROW_COUNTS.items():
            for org, rows in counts.items():
                ch_client.command(
                    INSERTS[table].format(database=dfe_db, table=table),
                    parameters={"anchor_ms": anchor_ms, "org": org, "rows": rows},
                )
        # A deploy fences the whole database, which also creates the tenant role every pinned user is granted.
        fence_tables(admin_client=ch_client, database=dfe_db)
        for statement in render_tier(tier=tier):
            ch_client.command(statement)
        for viewer, (user, password) in logins.items():
            for statement in render_pinned_user(
                org_ids=VIEWER_ORGS[viewer],
                pw_hash=hashlib.sha256(password.encode()).hexdigest(),
                tier_role=tier.role(),
                user=user,
            ):
                ch_client.command(statement)
        yield Estate(anchor_ms=anchor_ms, database=dfe_db, logins=logins, params=ch_params)
    finally:
        drop_database_and_policies(client=ch_client, database=dfe_db)
        for user, _ in logins.values():
            _drop_quietly(client=ch_client, statement=f"DROP USER IF EXISTS `{user}`")
        _drop_quietly(client=ch_client, statement=f"DROP ROLE IF EXISTS `{tier.role()}`")


def test_member_rows_read_through_all_sources(ch_client: Any, estate: Estate):
    members = _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])

    observed = {
        "members": members,
        "rows": _rows(client=ch_client, sql=ROWS_BY_TABLE.format(database=estate.database)),
    }

    assert observed == {"members": MEMBERS_EXPECTED, "rows": ROWS_BY_TABLE_EXPECTED}


def test_a_column_a_member_lacks_reads_as_its_default(ch_client: Any, estate: Estate):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])

    result = ch_client.query(DEFAULT_FILL.format(database=estate.database))
    observed = {row.pop("table"): row for row in result.named_results()}

    assert observed == DEFAULT_FILL_EXPECTED


# Pins a ClickHouse behaviour: IS NULL reads the `_raw.null` subcolumn, which a member without `_raw` fills with 0, while `_raw` itself reads NULL.
def test_raw_is_null_reads_a_member_without_raw_as_not_null(ch_client: Any, estate: Estate):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])

    observed = {
        "IS NOT NULL": _rows(
            client=ch_client, sql=RAW_NOT_NULL_ROWS.format(database=estate.database, settings="")
        ),
        "IS NULL": _rows(
            client=ch_client, sql=RAW_NULL_ROWS.format(database=estate.database, settings="")
        ),
        "IS NULL without the subcolumn rewrite": _rows(
            client=ch_client,
            sql=RAW_NULL_ROWS.format(database=estate.database, settings=NO_SUBCOLUMN_REWRITE),
        ),
    }

    assert observed == {
        "IS NOT NULL": [(MINIMAL, 7), (PASSTHROUGH, 11), (LANDING, 3)],
        "IS NULL": [],
        "IS NULL without the subcolumn rewrite": [(MINIMAL, 7), (PASSTHROUGH, 11)],
    }


def test_a_member_with_other_header_types_reads_through_all_sources(ch_client: Any, estate: Estate):
    database = estate.database
    ch_client.command(TYPED_TABLE.format(database=database, table=TYPED))
    ch_client.command(
        TYPED_INSERT.format(database=database, table=TYPED),
        parameters={"anchor_ms": estate.anchor_ms, "org": ORG_A},
    )
    fence_tables(admin_client=ch_client, database=database)
    _rebuild(client=ch_client, database=database, names=[MINIMAL, PASSTHROUGH, TYPED])

    observed = {
        "lag-max": _rows(
            client=ch_client, sql=_tile_sql(database=database, tile=_tiles()["lag-max"])
        ),
        "orga": _rows_as(
            estate=estate, sql=ROWS_BY_TABLE_AND_ORG.format(database=database), viewer=ORG_A
        ),
        "rows": [
            _relative(anchor_ms=estate.anchor_ms, row=row)
            for row in _rows(
                client=ch_client,
                parameters={"table": TYPED},
                sql=TYPED_ROWS.format(database=database),
            )
        ],
    }

    typed_row = (TYPED, 0, 1000, ORG_A, "typed", "typed-raw", 2)
    assert observed == {
        "lag-max": [(2000,)],
        "orga": [
            (MINIMAL, ORG_A, 3),
            (PASSTHROUGH, ORG_A, 5),
            (TYPED, ORG_A, 2),
            (LANDING, ORG_A, 2),
        ],
        "rows": [typed_row, typed_row],
    }


def test_a_name_that_extends_a_member_name_is_not_read(ch_client: Any, estate: Estate):
    database = estate.database
    for statement in LOOKALIKES:
        ch_client.command(statement.format(database=database))

    members = _rebuild(client=ch_client, database=database, names=[MINIMAL, PASSTHROUGH])
    observed = {
        "lookalike rows": _rows(client=ch_client, sql=LOOKALIKE_ROWS.format(database=database)),
        "member pattern": _member_pattern(client=ch_client, database=database),
        "members": members,
        "rows": _rows(client=ch_client, sql=ROWS_BY_TABLE.format(database=database)),
    }

    assert observed == {
        "lookalike rows": [(3, 7, 3)],
        "member pattern": f"^({MINIMAL}|{PASSTHROUGH}|{LANDING})$",
        "members": MEMBERS_EXPECTED,
        "rows": ROWS_BY_TABLE_EXPECTED,
    }


def test_a_source_named_after_a_manifest_table_stays_out_of_the_members(
    ch_client: Any, estate: Estate
):
    database = estate.database
    live = _rows(
        client=ch_client,
        parameters={"db": database, "tbl": DETECTION},
        sql="SELECT count() FROM system.tables WHERE database = {db:String} AND name = {tbl:String} AND engine LIKE '%MergeTree'",
    )

    members = _rebuild(client=ch_client, database=database, names=[DETECTION, MINIMAL, PASSTHROUGH])

    assert {"live": live, "members": members} == {"live": [(1,)], "members": MEMBERS_EXPECTED}


def test_each_tenant_reads_only_its_own_rows(ch_client: Any, estate: Estate):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])

    observed = _reads(estate=estate, sql=ROWS_BY_TABLE_AND_ORG.format(database=estate.database))

    assert observed == TENANT_ROWS_EXPECTED


def test_a_tenant_cannot_override_its_pin_reading_all_sources(ch_client: Any, estate: Estate):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])
    sql = f"{ROWS_BY_TABLE_AND_ORG.format(database=estate.database)} SETTINGS {TENANT_SETTING} = '{ORG_B}'"

    with pytest.raises(DatabaseError, match=r"Code: 452\. .*\(SETTING_CONSTRAINT_VIOLATION\)"):
        _rows_as(estate=estate, sql=sql, viewer=ORG_A)


# all_sources' policy on `_org_id` reaches every member read, so a member without the column would fail every tenant's query.
def test_a_member_without_org_id_stays_out_and_every_tenant_still_reads(
    ch_client: Any, estate: Estate
):
    database = estate.database
    _add_member_without_org_id(client=ch_client, estate=estate)

    members = _rebuild(client=ch_client, database=database, names=[MINIMAL, NO_ORG, PASSTHROUGH])
    observed = {
        "members": members,
        "reads": _reads(estate=estate, sql=ROWS_BY_TABLE_AND_ORG.format(database=database)),
    }

    assert observed == {"members": MEMBERS_EXPECTED, "reads": TENANT_ROWS_EXPECTED}


def test_a_member_without_org_id_shows_a_tenant_none_of_its_rows_through_its_deny_policy(
    ch_client: Any, estate: Estate
):
    database = estate.database
    _add_member_without_org_id(client=ch_client, estate=estate)
    # A rebuild leaves the member out, so all_sources is built over it here with no policy of its own.
    _replace_without_fence(client=ch_client, database=database, members=[*MEMBERS_EXPECTED, NO_ORG])

    observed = {
        "filter": _policy_filters(client=ch_client, database=database, table=NO_ORG),
        "reads": _reads(estate=estate, sql=ROWS_OF_TABLE.format(database=database, table=NO_ORG)),
    }

    assert observed == {
        "filter": ["0"],
        "reads": {ORG_A: [(0,)], ORG_B: [(0,)], UNRESTRICTED: [(4,)]},
    }


def test_member_policies_fence_a_merge_read_that_has_no_policy_of_its_own(
    ch_client: Any, estate: Estate
):
    _replace_without_fence(client=ch_client, database=estate.database, members=MEMBERS_EXPECTED)

    observed = {
        "policies": {
            table: _policies(client=ch_client, database=estate.database, table=table)
            for table in [*MEMBERS_EXPECTED, ALL_SOURCES]
        },
        **_reads(estate=estate, sql=ROWS_BY_TABLE_AND_ORG.format(database=estate.database)),
    }

    fenced = {
        table: [tenant_policy_name(db=estate.database, table=table)] for table in MEMBERS_EXPECTED
    }
    assert observed == {"policies": {**fenced, ALL_SOURCES: []}, **TENANT_ROWS_EXPECTED}


def test_a_rebuild_fences_all_sources_and_no_other_table(ch_client: Any, estate: Estate):
    database = estate.database
    _drop_policy(client=ch_client, database=database, table=PASSTHROUGH)
    before = _database_policies(client=ch_client, database=database)

    _rebuild(client=ch_client, database=database, names=[MINIMAL, PASSTHROUGH])
    after = _database_policies(client=ch_client, database=database)

    assert {"added": sorted(after - before), "removed": sorted(before - after)} == {
        "added": [tenant_policy_name(db=database, table=ALL_SOURCES)],
        "removed": [],
    }


def test_the_all_sources_policy_outlives_a_replace_and_a_second_rebuild(
    ch_client: Any, estate: Estate
):
    sql = ROWS_BY_TABLE_AND_ORG.format(database=estate.database)
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])
    # The members' own policies also fence a merge read, so they go to leave all_sources' policy as the only fence.
    for table in MEMBERS_EXPECTED:
        _drop_policy(client=ch_client, database=estate.database, table=table)

    _replace_without_fence(client=ch_client, database=estate.database, members=MEMBERS_EXPECTED)
    replaced = {
        "policies": _policies(client=ch_client, database=estate.database, table=ALL_SOURCES),
        "reads": _reads(estate=estate, sql=sql),
    }
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])
    rebuilt = {
        "policies": _policies(client=ch_client, database=estate.database, table=ALL_SOURCES),
        "reads": _reads(estate=estate, sql=sql),
    }

    expected = {
        "policies": [tenant_policy_name(db=estate.database, table=ALL_SOURCES)],
        "reads": TENANT_ROWS_EXPECTED,
    }
    assert {"rebuilt": rebuilt, "replaced": replaced} == {"rebuilt": expected, "replaced": expected}


def test_a_source_left_out_of_the_registry_drops_out_on_the_next_rebuild(
    ch_client: Any, estate: Estate
):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])

    members = _rebuild(client=ch_client, database=estate.database, names=[MINIMAL])
    observed = {
        "members": members,
        "rows": _rows(client=ch_client, sql=ROWS_BY_TABLE.format(database=estate.database)),
        "source table rows": _rows(
            client=ch_client, sql=f"SELECT count() FROM `{estate.database}`.`{PASSTHROUGH}`"
        ),
    }

    assert observed == {
        "members": [MINIMAL, LANDING],
        "rows": [(MINIMAL, 7), (LANDING, 3)],
        "source table rows": [(11,)],
    }


def test_dashboard_tiles_never_read_a_member_default_as_a_real_time(ch_client: Any, estate: Estate):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])
    tiles = _tiles()

    observed = {
        tile_id: [
            _relative(anchor_ms=estate.anchor_ms, row=row)
            for row in _rows(
                client=ch_client, sql=_tile_sql(database=estate.database, tile=tiles[tile_id])
            )
        ]
        for tile_id in DASHBOARD_TILES
    }

    assert observed == DASHBOARD_EXPECTED


def test_the_lag_where_is_what_keeps_the_epoch_rows_out(ch_client: Any, estate: Estate):
    _rebuild(client=ch_client, database=estate.database, names=[MINIMAL, PASSTHROUGH])
    tile = _tiles()["lag-max"]
    unguarded = {**tile, "config": {**tile["config"], "where": ""}}

    [(lag_ms,)] = _rows(client=ch_client, sql=_tile_sql(database=estate.database, tile=unguarded))

    assert lag_ms > FIFTY_YEARS_MS


@pytest.mark.parametrize(
    "case", BOOTSTRAP_LOADER_CASES, ids=[case["id"] for case in BOOTSTRAP_LOADER_CASES]
)
def test_a_converged_bootstrap_builds_all_sources_from_its_loader(
    bootstrap_pass: Callable[..., SchemaBootstrapState],
    ch_client: Any,
    clickhouse_test_database: str,
    case: BootstrapLoaderCase,
):
    state = bootstrap_pass(
        bootstrap_tables=True, database=clickhouse_test_database, load_sources=case["load_sources"]
    )

    observed = {
        **_all_sources_state(client=ch_client, database=clickhouse_test_database),
        "state": state.state,
    }

    assert observed == case["expected"]


def test_a_converged_bootstrap_with_an_unreadable_registry_builds_nothing_and_carries_on(
    bootstrap_pass: Callable[..., SchemaBootstrapState],
    ch_client: Any,
    clickhouse_test_database: str,
):
    def unreadable_registry() -> list[Source]:
        """A registry loader whose store cannot be read."""
        raise RuntimeError("registry store unreachable")

    state = bootstrap_pass(
        bootstrap_tables=True, database=clickhouse_test_database, load_sources=unreadable_registry
    )

    observed = {
        **_all_sources_state(client=ch_client, database=clickhouse_test_database),
        "state": state.state,
    }

    assert observed == {"all_sources policies": 0, "member pattern": None, "state": "converged"}


def test_a_bootstrap_switched_off_builds_all_sources_over_the_existing_landing_table(
    bootstrap_pass: Callable[..., SchemaBootstrapState], ch_client: Any, dfe_db: str
):
    state = bootstrap_pass(bootstrap_tables=False, database=dfe_db, load_sources=tuple)

    observed = {**_all_sources_state(client=ch_client, database=dfe_db), "state": state.state}

    assert observed == {"all_sources policies": 1, "member pattern": "^(main)$", "state": "unknown"}


def test_a_source_deployed_through_the_api_joins_all_sources(
    ch_client: Any, stack: SimpleNamespace
):
    database = stack.database
    deploy_source(name=DEPLOYED, stack=stack)
    ch_client.command(
        f"INSERT INTO `{database}`.`{DEPLOYED}` (_org_id, _json) SELECT '{ORG_A}', '{{}}' FROM numbers(2)"
    )

    observed = {
        "member pattern": _member_pattern(client=ch_client, database=database),
        "policies": _policies(client=ch_client, database=database, table=ALL_SOURCES),
        "rows": _rows(client=ch_client, sql=ROWS_BY_TABLE.format(database=database)),
    }

    assert observed == {
        "member pattern": f"^({DEPLOYED}|{LANDING})$",
        "policies": [tenant_policy_name(db=database, table=ALL_SOURCES)],
        "rows": [(DEPLOYED, 2)],
    }


def test_deleting_sources_through_the_api_drops_them_from_all_sources(
    ch_client: Any, stack: SimpleNamespace
):
    database = stack.database
    at_boot = _member_pattern(client=ch_client, database=database)
    for name in (BULK_A, BULK_B, DEPLOYED):
        deploy_source(name=name, stack=stack)
    deployed = _member_pattern(client=ch_client, database=database)

    deleted = stack.client.delete(f"/api/v1/sources/{DEPLOYED}", headers=stack.headers)
    after_delete = _member_pattern(client=ch_client, database=database)
    bulk = stack.client.post(
        "/api/v1/sources/bulk",
        headers=stack.headers,
        json={"action": "delete", "sources": [BULK_A, BULK_B]},
    )
    after_bulk = _member_pattern(client=ch_client, database=database)
    observed = {
        "at boot": at_boot,
        "bulk delete": (bulk.status_code, bulk.json()["succeeded"]),
        "delete": deleted.status_code,
        "member patterns": [deployed, after_delete, after_bulk],
        "tables kept": _rows(
            client=ch_client,
            parameters={"db": database, "names": [BULK_A, BULK_B, DEPLOYED]},
            sql="SELECT name FROM system.tables WHERE database = {db:String} AND name IN {names:Array(String)} ORDER BY name",
        ),
    }

    assert observed == {
        "at boot": f"^({LANDING})$",
        "bulk delete": (200, [BULK_A, BULK_B]),
        "delete": 200,
        "member patterns": [
            f"^({BULK_A}|{BULK_B}|{DEPLOYED}|{LANDING})$",
            f"^({BULK_A}|{BULK_B}|{LANDING})$",
            f"^({LANDING})$",
        ],
        "tables kept": [(BULK_A,), (BULK_B,), (DEPLOYED,)],
    }
