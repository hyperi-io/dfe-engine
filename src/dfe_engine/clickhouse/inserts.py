#  Project:      dfe-engine
#  File:         clickhouse/inserts.py
#  Purpose:      Insert settings for a writer whose identical rows must all land
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Settings that stop a Replicated table dropping an insert identical to a recent one.

Every DFE table is a Replicated engine, and those drop an insert whose data is
byte-identical to a recent one. A version column the server fills in, such as a
``DEFAULT now64(3)`` timestamp, is not part of what is compared. So a writer that
re-sends a row it sent before, and needs the new copy to win, loses it.

``deduplicate_insert`` overrides ``insert_deduplicate`` where the server has it
(26.3 does), and a server without it refuses the name, so it is sent only where
``system.settings`` lists it.
"""

from typing import Any

__all__ = ["keep_identical_rows"]


def keep_identical_rows(client: Any) -> dict[str, Any]:
    """The insert settings under which a row identical to a recent one still lands.

    Args:
        client: A clickhouse-connect client or the engine's wrapper of one.

    Returns:
        ``insert_deduplicate = 0``, and ``deduplicate_insert = 'disable'`` where the
        server knows that setting.
    """
    known = client.query(
        "SELECT count() FROM system.settings WHERE name = {name:String}",
        parameters={"name": "deduplicate_insert"},
    ).result_rows
    settings: dict[str, Any] = {"insert_deduplicate": 0}
    if known and int(known[0][0]):
        settings["deduplicate_insert"] = "disable"
    return settings
