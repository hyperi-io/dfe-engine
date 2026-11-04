#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_worker_safe_horizon.py
#  Purpose:      The safe horizon hold, the overlap read and the dedupe conjunct a fire binds
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a fire does with a lagging replica, and what it reads once it runs.

The real exactly-once behaviour is proved on three ClickHouse replicas in
``tests/integration/test_hunt_safe_horizon.py``. These pin the decisions in between:
when a fire is held, when the cap lets it go, and the window each statement binds.
"""

from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from dfe_engine.hunt_runner import HuntSpec, HuntStatement, HuntWorker
from dfe_engine.hunt_runner.metrics import HuntRunnerMetrics

from .conftest import Observation

RULE = HuntStatement(
    sql="INSERT INTO `d`.`det` SELECT _uuid FROM `d`.`src` WHERE {window} AND {unseen}",
    rule_id="r1",
    source=("d", "src"),
    target=("d", "det"),
)
RULES_HUNT = HuntSpec(hunt_id="h", interval_seconds=600, queries=[RULE], customers=["acme"])
DIRECT_HUNT = HuntSpec(
    hunt_id="direct",
    interval_seconds=600,
    queries=["INSERT INTO d.det SELECT * FROM d.src WHERE {window}"],
)


class _Ch:
    """A ClickHouse that reports one replication delay and records every statement."""

    def __init__(self, *, now: int, delay: int = 0, broken: bool = False) -> None:
        self.now = now
        self.delay = delay
        self.broken = broken
        self.queries: list[str] = []
        self.commands: list[str] = []

    def query(self, sql: str, settings=None) -> SimpleNamespace:
        self.queries.append(sql)
        if "system.replicas" in sql:
            if self.broken:
                raise DatabaseError("Code: 497. ACCESS_DENIED")
            return SimpleNamespace(result_rows=[(self.now, self.delay)])
        return SimpleNamespace(result_rows=[(self.now * 1000, 0, 0)])

    def command(self, sql: str, settings=None) -> SimpleNamespace:
        self.commands.append(sql)
        return SimpleNamespace(written_rows=0)


class _Coordinator:
    def __init__(self, watermark: int | None) -> None:
        self.watermark = watermark

    def get_watermark(self, hunt_id: str) -> int | None:
        return self.watermark

    def set_watermark(self, hunt_id: str, watermark: int) -> None:
        self.watermark = watermark

    def record_run(self, hunt_id: str, fire: int, rows_written: int) -> None:
        pass


def _worker(ch: _Ch, *, manager=None, watermark: int | None = None, **kwargs) -> HuntWorker:
    metrics = HuntRunnerMetrics(manager) if manager is not None else None
    return HuntWorker(ch, _Coordinator(watermark), metrics=metrics, **kwargs)  # ty: ignore[invalid-argument-type]  # stand-ins carrying only the calls this path makes


@pytest.mark.parametrize(
    ("now", "delay", "held"),
    [
        pytest.param(1000, 0, False, id="quiet_table"),
        pytest.param(1100, 60, False, id="horizon_above_fire"),
        pytest.param(1030, 60, True, id="horizon_below_fire"),
        pytest.param(1299, 400, True, id="just_inside_the_cap"),
        pytest.param(1300, 400, False, id="at_the_cap"),
    ],
)
def test_a_fire_is_held_only_while_the_horizon_is_short_and_under_the_cap(now, delay, held):
    worker = _worker(_Ch(now=now, delay=delay), max_deferral_seconds=300)

    assert worker.hold_for_safe_horizon(RULES_HUNT, 1000) is held


def test_the_cap_is_never_longer_than_the_interval():
    short = RULES_HUNT.model_copy(update={"interval_seconds": 60})
    worker = _worker(_Ch(now=1060, delay=400), max_deferral_seconds=300)

    assert worker.hold_for_safe_horizon(short, 1000) is False


@pytest.mark.parametrize(
    ("spec", "kwargs"),
    [
        pytest.param(DIRECT_HUNT, {}, id="direct_query_names_no_table"),
        pytest.param(RULES_HUNT, {"safe_horizon": False}, id="safe_horizon_off"),
    ],
)
def test_a_fire_with_no_safe_horizon_to_read_reads_nothing(spec, kwargs):
    ch = _Ch(now=1000, delay=900)

    assert _worker(ch, **kwargs).hold_for_safe_horizon(spec, 1000) is False
    assert ch.queries == []


def test_an_unreadable_safe_horizon_holds_nothing():
    assert (
        _worker(_Ch(now=1000, delay=900, broken=True)).hold_for_safe_horizon(RULES_HUNT, 1000)
        is False
    )


def test_deferrals_are_counted_per_poll_and_per_fire(manager):
    ch = _Ch(now=1010, delay=60)
    worker = _worker(ch, manager=manager)
    assert worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    assert worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    ch.now, ch.delay = 1100, 0

    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    assert len(manager.observed("hunt_safe_horizon_deferrals_total")) == 2
    assert manager.observed("hunt_safe_horizon_deferrals_per_fire") == [
        Observation("hunt_safe_horizon_deferrals_per_fire", {"hunt_id": "h"}, "observe", 2)
    ]
    assert manager.observed("hunt_safe_horizon_cap_reached_total") == []


def test_a_fire_let_go_by_the_cap_is_counted(manager):
    worker = _worker(_Ch(now=1300, delay=900), manager=manager, max_deferral_seconds=300)

    assert not worker.hold_for_safe_horizon(RULES_HUNT, 1000)
    assert manager.observed("hunt_safe_horizon_cap_reached_total") == [
        Observation("hunt_safe_horizon_cap_reached_total", {"hunt_id": "h"}, "inc", 1)
    ]


@pytest.mark.parametrize(
    ("watermark", "read_start"),
    [
        pytest.param(None, 400, id="first_run_reads_its_lookback_only"),
        pytest.param(950, 650, id="resumed_run_reads_the_overlap"),
    ],
)
def test_a_rule_reads_the_overlap_and_skips_what_it_detected_over_the_same_window(
    watermark, read_start
):
    ch = _Ch(now=1000)
    _worker(ch, watermark=watermark, overlap_seconds=300).run(RULES_HUNT, 1000)

    [sql] = ch.commands
    window = (
        f"(_timestamp_load >= toDateTime64({read_start}, 3) "
        "AND _timestamp_load < toDateTime64(1000, 3))"
    )
    assert sql == (
        f"INSERT INTO `d`.`det` SELECT _uuid FROM `d`.`src` WHERE {window} AND "
        "_uuid NOT IN (SELECT matched_uuid FROM `d`.`det` "
        f"WHERE hunt_name = 'h' AND rule_id = 'r1' AND {window})"
    )


def test_a_direct_query_cannot_skip_what_it_saw_so_it_reads_no_overlap():
    ch = _Ch(now=1000)
    _worker(ch, watermark=950, overlap_seconds=300).run(DIRECT_HUNT, 1000)

    assert ch.commands == [
        "INSERT INTO d.det SELECT * FROM d.src WHERE "
        "(_timestamp_load >= toDateTime64(950, 3) AND _timestamp_load < toDateTime64(1000, 3))"
    ]


def test_the_watermark_still_advances_to_the_fire():
    coord = _Coordinator(950)
    HuntWorker(_Ch(now=1000), coord, overlap_seconds=300).run(RULES_HUNT, 1000)  # ty: ignore[invalid-argument-type]  # stand-ins carrying only the calls this path makes

    assert coord.watermark == 1000
