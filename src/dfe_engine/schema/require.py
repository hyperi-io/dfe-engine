#  Project:      dfe-engine
#  File:         schema/require.py
#  Purpose:      Assert a manifest object is present, for the paths that only read it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Assert the tables a runtime path needs, rather than creating them.

The hunt runner, the repository store and the alert-cooldown path each used to
create their own tables through the applier. Three appliers against one database
race on ``ALTER TABLE ADD COLUMN``, and each was a second definition of when an
object comes into existence. The schema phase is the one that makes them, so
these paths assert and fail loud instead.

Failing loud is the point: a missing coordination table means the phase has not
run or did not converge, and a path that silently created its own would hide
that from the operator reading the schema status route.
"""

from typing import Any

from clickhouse_connect.driver.exceptions import ClickHouseError

from dfe_engine.schema.plan import object_names


class SchemaNotAppliedError(Exception):
    """An object the manifest declares is not on the server.

    The engine's schema phase creates it at boot, so this means the phase has not
    run here, was switched off, or did not converge.
    """


def missing_objects(client: Any, *, database: str, object_ids: tuple[str, ...]) -> list[str]:
    """The named manifest objects absent from *database*, as ``database.name``, sorted.

    Args:
        client: A live ClickHouse client exposing ``query``.
        database: The database the objects land in.
        object_ids: Manifest ids, so no caller restates a table name.

    Returns:
        Every absent object, empty when all are there.

    Raises:
        clickhouse_connect.driver.exceptions.ClickHouseError: The server could not
            be read, as the client raises it.
        OSError: The connection failed below the driver.
    """
    names = object_names(*object_ids, data_database=database)
    wanted = sorted(names.values())
    rows = client.query(
        "SELECT name FROM system.tables WHERE database = {db:String} AND name IN {names:Array(String)}",
        parameters={"db": database, "names": wanted},
    ).result_rows
    present = {str(row[0]) for row in rows}
    return [f"{database}.{name}" for name in wanted if name not in present]


def require_objects(client: Any, *, database: str, object_ids: tuple[str, ...], what: str) -> None:
    """Assert every named manifest object exists in *database*.

    Args:
        client: A live ClickHouse client exposing ``query``.
        database: The database the objects land in.
        object_ids: Manifest ids, so no caller restates a table name.
        what: What the caller needs them for, named in the error.

    Raises:
        SchemaNotAppliedError: One or more of them is absent, or the server could
            not be read.
    """
    try:
        missing = missing_objects(client, database=database, object_ids=object_ids)
    except (ClickHouseError, OSError) as exc:
        raise SchemaNotAppliedError(f"could not check {what} in {database}: {exc}") from exc
    if missing:
        raise SchemaNotAppliedError(
            f"{what} needs {', '.join(missing)}, which the engine's schema phase creates at "
            "boot; check GET /api/v1/system/schema"
        )
