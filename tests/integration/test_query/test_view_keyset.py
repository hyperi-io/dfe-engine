#  Project:      dfe-engine
#  File:         tests/integration/test_query/test_view_keyset.py
#  Purpose:      Keyset paging over a real parameterised view walks every row once
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Keyset paging through ``ViewExecutor`` against a real ClickHouse.

The views are discovered by the real ``ViewCatalog`` and run by the real client, so
each page is the SQL and the bound cursor exactly as a request sends them. A walk
must return every tenant row once, in key order, and stop on an empty page.
"""

import pytest

from dfe_engine.query.catalog import ViewCatalog
from dfe_engine.query.executor import ViewExecutor, ViewRequestError
from dfe_engine.query.models import AuthContext, QueryOptions

pytestmark = pytest.mark.integration

# (n, ts, s, id) for tenant acme. n ties at 2, so only the n+id pair is unique.
ROWS = [
    (1, "2024-01-15 11:00:00.000", "apple", "e1"),
    (2, "2024-01-15 12:00:00.000", "banana", "e2"),
    (2, "2024-01-15 12:00:00.500", "cherry", "e3"),
    (3, "2024-01-15 13:00:00.000", "date", "e4"),
    (5, "2024-01-15 14:00:00.000", "elder", "e5"),
]


@pytest.fixture
def executor(clickhouse_client, clickhouse_test_database) -> ViewExecutor:
    clickhouse_client.command(
        "CREATE TABLE probe_events (org String, n UInt64, ts DateTime64(3, 'UTC'), "
        "s String, id String) ENGINE = Memory"
    )
    values = ", ".join(f"('acme', {n}, '{ts}', '{s}', '{i}')" for n, ts, s, i in ROWS)
    clickhouse_client.command(
        f"INSERT INTO probe_events VALUES {values}, ('other', 4, '2024-01-15 12:30:00', 'fig', 'x9')"
    )
    clickhouse_client.command(
        "CREATE VIEW dfe_v_probe_events AS SELECT n, ts, s, id FROM probe_events "
        "WHERE org = {org_id:String}"
    )
    clickhouse_client.command(
        "CREATE VIEW dfe_v_probe_capped AS SELECT n, id FROM probe_events "
        "WHERE org = {org_id:String} ORDER BY n LIMIT {limit:UInt32}"
    )
    catalog = ViewCatalog(client=clickhouse_client, database=clickhouse_test_database)
    return ViewExecutor(
        restricted_client=clickhouse_client, catalog=catalog, database=clickhouse_test_database
    )


AUTH = AuthContext(org_id="acme", user_id="probe", roles=["admin"], request_id="keyset")


def _walk(executor: ViewExecutor, **keyset) -> list[dict]:
    """Every row the keyset walk returns, two to a page, following the last row each time."""
    seen: list[dict] = []
    cursor: dict = {}
    for _ in range(10):
        options = QueryOptions(limit=2, **keyset, **cursor)
        rows = executor.execute("probe/events", {}, AUTH, options).rows
        if not rows:
            return seen
        seen.extend(rows)
        last = rows[-1]
        cursor = {"after_key": _cursor_value(last[keyset["order_by"]])}
        if keyset.get("tiebreak_by"):
            cursor["after_tiebreak"] = last[keyset["tiebreak_by"]]
    raise AssertionError("the walk never reached an empty page")


def _cursor_value(value):
    """The cursor a client sends back: numbers as-is, timestamps in the documented ISO form."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f") if hasattr(value, "strftime") else value


@pytest.mark.parametrize(
    ("column", "order_dir", "expected"),
    [
        ("ts", "asc", ["e1", "e2", "e3", "e4", "e5"]),
        ("ts", "desc", ["e5", "e4", "e3", "e2", "e1"]),
        ("s", "asc", ["e1", "e2", "e3", "e4", "e5"]),
    ],
)
def test_a_unique_key_walks_every_row_once(executor, column, order_dir, expected):
    rows = _walk(executor, order_by=column, order_dir=order_dir)

    assert [row["id"] for row in rows] == expected


def test_an_integer_key_with_ties_walks_every_row_with_a_tiebreak(executor):
    rows = _walk(executor, order_by="n", tiebreak_by="id")

    assert [row["id"] for row in rows] == ["e1", "e2", "e3", "e4", "e5"]


def test_an_integer_key_with_ties_skips_the_tied_row_without_a_tiebreak(executor):
    """The documented limit of a single non-unique key: a row tying with the cursor is lost.

    The sort is unstable on ties, so which of e2 and e3 lands on the first page varies.
    """
    ids = [row["id"] for row in _walk(executor, order_by="n")]

    assert ids[0] == "e1"
    assert ids[2:] == ["e4", "e5"]
    assert ids[1] in {"e2", "e3"}


def test_the_documented_timestamp_cursor_parses(executor):
    options = QueryOptions(order_by="ts", after_key="2024-01-15T12:00:00", limit=10)

    rows = executor.execute("probe/events", {}, AUTH, options).rows

    assert [row["id"] for row in rows] == ["e3", "e4", "e5"]


def test_a_cursor_that_does_not_parse_as_its_column_is_a_request_error(executor):
    options = QueryOptions(order_by="n", after_key="not-a-number", limit=10)

    with pytest.raises(ViewRequestError, match="does not parse as its column's type"):
        executor.execute("probe/events", {}, AUTH, options)


def test_a_view_that_caps_its_own_rows_refuses_paging(executor):
    with pytest.raises(ViewRequestError, match="caps its own rows"):
        executor.execute("probe/capped", {}, AUTH, QueryOptions(limit=2, offset=2))

    first = executor.execute("probe/capped", {}, AUTH, QueryOptions(limit=2)).rows
    assert [row["id"] for row in first] == ["e1", "e2"]
