#  Project:      dfe-engine
#  File:         hunts/hunt_target_check.py
#  Purpose:      Hold a hunt's results table to the columns the runner writes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where a hunt writes its detections, and whether that table takes what the runner inserts.

A hunt writes to ``<data database>.detection`` unless it or a rule entry names a results
table. A table missing a column the runner's INSERT names fails at the first run, not at the
save, so the save reads the live columns from ``system.columns`` and refuses the gap.
"""

from typing import Any

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.schema.applier import live_columns

DETECTION_TABLE = "detection"
"""The core table detections land in when a hunt names no other."""


def missing_columns(client: Any, database: str, table: str) -> list[str] | None:
    """The columns the runner's INSERT names that the table does not have.

    Args:
        client: A ClickHouse client.
        database: The target's database.
        table: The target's table.

    Returns:
        The missing columns in INSERT order, empty when the table takes every one, or None
        when the table does not exist.

    Raises:
        SchemaApplyError: ClickHouse could not be read.
    """
    present = live_columns(client=client, database=database, table=table)
    if not present:
        return None
    return [column for column in HuntResultSchema().insert_columns() if column not in present]
