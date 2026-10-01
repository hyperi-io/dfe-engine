#  Project:      dfe-engine
#  File:         tests/integration/test_clickhouse_quoting.py
#  Purpose:      Quoted identifiers name the column they were given, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A name quoted by the engine reaches ClickHouse as exactly that name.

ClickHouse reads C-style escapes inside a backtick-quoted identifier, so a name
holding a backslash only survives if the backslash is escaped. ``system.columns``
reports what the server actually stored, which is the check a text comparison of
the generated SQL cannot make.
"""

import pytest

from dfe_engine.clickhouse.quoting import column_reference, quote_identifier

pytestmark = pytest.mark.integration

# Plain, whitespace, an embedded backtick, a backslash, and a backslash before a backtick.
NAMES = ["plain", "has space", "we`ird", "a\\b", "back\\`tick"]
TABLE = "quoting_probe"


@pytest.fixture
def probe_table(clickhouse_client, clickhouse_test_database) -> str:
    columns = ", ".join(f"{quote_identifier(name)} String" for name in NAMES)
    clickhouse_client.command(f"CREATE TABLE {quote_identifier(TABLE)} ({columns}) ENGINE = Memory")
    clickhouse_client.command(
        f"INSERT INTO {quote_identifier(TABLE)} VALUES ('p', 's', 'w', 'b', 't')"
    )
    return clickhouse_test_database


def test_every_quoted_name_is_stored_as_written(clickhouse_client, probe_table):
    stored = clickhouse_client.query(
        "SELECT name, length(name) FROM system.columns"
        " WHERE database = {db:String} AND table = {table:String} ORDER BY position",
        parameters={"db": probe_table, "table": TABLE},
    ).result_rows

    assert [row[0] for row in stored] == NAMES
    assert dict(stored)["a\\b"] == 3


@pytest.mark.parametrize(
    ("name", "value"), [("has space", "s"), ("we`ird", "w"), ("a\\b", "b"), ("back\\`tick", "t")]
)
def test_a_column_reference_reads_its_own_column(clickhouse_client, probe_table, name, value):
    row = clickhouse_client.query(
        f"SELECT {column_reference(name)} FROM {quote_identifier(TABLE)}"
    ).result_rows

    assert row == [(value,)]
