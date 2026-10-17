#  Project:      dfe-engine
#  File:         tests/support/clickhouse_cleanup.py
#  Purpose:      Remove a test database and everything the server keeps outside it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Remove a test database from a ClickHouse server that outlives the test run.

A docker container is thrown away with everything in it. A configured cluster is not,
and ``DROP DATABASE`` leaves the row policies on the database's tables behind, each named
for a database that no longer exists. The tenant fence a source deploy applies creates one
per table.
"""

from typing import Any


def _quoted(name: str) -> str:
    """*name* as a backtick-quoted ClickHouse identifier."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def drop_database_and_policies(client: Any, database: str) -> None:
    """Drop the row policies on *database*'s tables, then the database itself.

    The policies go first, while the tables they name still exist. Only a database
    this test created and named for itself belongs here: the policies dropped are every
    one the server holds for it.

    Args:
        client: A clickhouse-connect client with access management.
        database: The test's own database.
    """
    policies = client.query(
        "SELECT short_name, `table` FROM system.row_policies WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    for short_name, table in policies:
        client.command(
            f"DROP ROW POLICY IF EXISTS {_quoted(short_name)} ON {_quoted(database)}.{_quoted(table)}"
        )
    client.command(f"DROP DATABASE IF EXISTS {_quoted(database)} SYNC")
