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

A Replicated database dropped on one server is dropped there alone, so on a cluster of
more than one server the drop goes ``ON CLUSTER``. Row policies need no fan-out: a
cluster keeps its access entities in Keeper, which dfe-schemas requires of it.
"""

from typing import Any

from dfe_engine.clickhouse.statements import DDL_OUTPUT_MODE


def _quoted(name: str) -> str:
    """*name* as a backtick-quoted ClickHouse identifier."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _replicated_on_a_cluster(client: Any, database: str) -> bool:
    """Whether *database* is a Replicated database on a cluster of more than one server."""
    rows = client.query(
        "SELECT (SELECT any(engine) FROM system.databases WHERE name = {db:String}), "
        "(SELECT count() FROM system.clusters WHERE cluster = "
        "(SELECT any(substitution) FROM system.macros WHERE macro = 'cluster'))",
        parameters={"db": database},
    ).result_rows
    engine, servers = rows[0]
    return engine == "Replicated" and int(servers) > 1


def drop_database_and_policies(client: Any, database: str) -> None:
    """Drop the row policies on *database*'s tables, then the database itself, on every replica.

    The policies go first, while the tables they name still exist. Only a database
    this test created and named for itself belongs here: the policies dropped are every
    one the server holds for it.

    Args:
        client: A clickhouse-connect client with access management.
        database: The test's own database.
    """
    fan_out = _replicated_on_a_cluster(client, database)
    policies = client.query(
        "SELECT short_name, `table` FROM system.row_policies WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    for short_name, table in policies:
        client.command(
            f"DROP ROW POLICY IF EXISTS {_quoted(short_name)} ON {_quoted(database)}.{_quoted(table)}"
        )
    if fan_out:
        # ON CLUSTER runs from the DDL task queue, which a replica down longer than
        # task_max_lifetime (7 days by default) misses, and the database stays there.
        client.command(
            f"DROP DATABASE IF EXISTS {_quoted(database)} ON CLUSTER '{{cluster}}' SYNC",
            settings=dict(DDL_OUTPUT_MODE),
        )
        return
    client.command(f"DROP DATABASE IF EXISTS {_quoted(database)} SYNC")
