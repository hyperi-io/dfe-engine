#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_worker_first_window.py
#  Purpose:      A hunt's first window is read whole however many fires it takes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A new hunt has no watermark, so its window starts an interval before the fire.

If that first fire never completes, the next fire must still start where the
first one would have, or the rows between the two fires are never read.
"""

import re
from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.hunts.hunt_spec import HuntSpec
from dfe_engine.hunts.worker import HuntWorker
from tests.unit.test_hunts.test_worker_safe_horizon import _Coordinator


class _FailsOnce:
    """A ClickHouse client whose first statement fails, recording every statement."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.failed = False

    def query(self, sql: str, parameters=None, settings=None) -> SimpleNamespace:
        return SimpleNamespace(result_rows=[[0]])

    def command(self, sql: str, settings=None) -> SimpleNamespace:
        self.commands.append(sql)
        if not self.failed:
            self.failed = True
            raise DatabaseError("Code: 241. DB::Exception: Memory limit exceeded")
        return SimpleNamespace(written_rows=0)


def _window_start(sql: str) -> int:
    match = re.search(r"_timestamp_load >= toDateTime64\((\d+)", sql)
    assert match, sql
    return int(match.group(1))


def test_the_window_a_failed_first_fire_meant_to_read_is_read_by_the_next_fire():
    ch = _FailsOnce()
    coord = _Coordinator(None)
    spec = HuntSpec(hunt_id="h", interval_seconds=600, queries=["SELECT 1 WHERE {window}"])
    worker = HuntWorker(ch, coord)  # ty: ignore[invalid-argument-type]  # stand-ins carrying only the calls this path makes

    with pytest.raises(DatabaseError):
        worker.run(spec, scheduled_start=1000)
    assert coord.watermark is None
    assert _window_start(ch.commands[0]) == 400

    worker.run(spec, scheduled_start=1600)
    # Not 1000: the rows between 400 and 1000 were never read.
    assert _window_start(ch.commands[1]) == 400
    assert coord.watermark == 1600
